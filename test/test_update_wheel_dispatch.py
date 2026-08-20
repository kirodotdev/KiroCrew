"""Tests for the CLI ``kirocrew update`` wheel-install dispatch (issue #1871).

Covers:
- Install layout detection (git, wheel, externally managed)
- Wheel update path: feed fetch, version comparison, installer invocation
- Externally managed installs print guidance instead of failing
"""

from __future__ import annotations

import json
import subprocess  # noqa: F401 -- used via monkeypatch.setattr
from unittest.mock import MagicMock

import pytest


def _init_repo(path) -> None:
    """Make *path* the top level of a real git working tree.

    Detection asks git and anchors the answer to this exact directory, so a
    fabricated ``.git`` entry does not stand in for a repository.
    """
    subprocess.run(
        ["git", "init", "-q"], cwd=str(path), check=True, capture_output=True, timeout=30
    )


class TestDetectInstallLayout:
    """Tests for platform/update_layout.detect_install_layout."""

    def test_git_checkout_detected(self, monkeypatch, tmp_path) -> None:
        proj = tmp_path / "project"
        proj.mkdir()
        _init_repo(proj)
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(proj))
        monkeypatch.setattr(
            "kiro_crew.platform.update_capability.running_from_checkout",
            lambda root, **kw: True,
        )

        from kiro_crew.platform.update_layout import detect_install_layout

        layout = detect_install_layout()
        assert layout.kind == "git"
        assert layout.is_git is True
        assert layout.is_externally_managed is False
        assert layout.proj == str(proj)

    def test_an_externally_managed_stamp_beats_a_mounted_checkout(
        self, monkeypatch, tmp_path
    ) -> None:
        """The layout must not call a container a git install.

        Operators do mount a checkout into a container. Classifying that as "git"
        made the channel endpoint refuse a switch with "a git checkout follows its
        git remote" instead of naming the surface that actually updates the install —
        and it is the same inverted precedence the CLI dispatch had, in a second
        consumer, which is exactly what deriving from one contract prevents.
        """
        proj = tmp_path / "project"
        proj.mkdir()
        _init_repo(proj)
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(proj))
        monkeypatch.setattr("kiro_crew.platform.update_capability.distribution", lambda: "docker")
        monkeypatch.setattr("kiro_crew.platform.update_layout.distribution", lambda: "docker")

        from kiro_crew.platform.update_layout import detect_install_layout

        layout = detect_install_layout()
        assert layout.kind == "docker"
        assert layout.is_git is False
        assert layout.is_externally_managed is True
        assert layout.guidance, "an externally managed layout must carry its guidance"

    def test_git_worktree_file_detected(self, monkeypatch, tmp_path) -> None:
        """A .git FILE (worktree/submodule) is still detected as git."""
        proj = tmp_path / "project"
        proj.mkdir()
        _init_repo(proj)
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(proj))
        monkeypatch.setattr(
            "kiro_crew.platform.update_capability.running_from_checkout",
            lambda root, **kw: True,
        )

        from kiro_crew.platform.update_layout import detect_install_layout

        layout = detect_install_layout()
        assert layout.kind == "git"
        assert layout.is_git is True

    def test_no_project_dir_falls_to_distribution(self, monkeypatch) -> None:
        monkeypatch.delenv("KIROCREW_PROJECT_DIR", raising=False)
        monkeypatch.setattr("kiro_crew.platform.update_layout.distribution", lambda: "wheel")

        from kiro_crew.platform.update_layout import detect_install_layout

        layout = detect_install_layout()
        assert layout.kind == "wheel"
        assert layout.is_git is False
        assert layout.is_externally_managed is False

    def test_dmg_is_externally_managed(self, monkeypatch) -> None:
        monkeypatch.delenv("KIROCREW_PROJECT_DIR", raising=False)
        monkeypatch.setattr("kiro_crew.platform.update_layout.distribution", lambda: "dmg")

        from kiro_crew.platform.update_layout import detect_install_layout

        layout = detect_install_layout()
        assert layout.kind == "dmg"
        assert layout.is_externally_managed is True
        assert "desktop app" in layout.guidance.lower()

    def test_docker_is_externally_managed(self, monkeypatch) -> None:
        monkeypatch.delenv("KIROCREW_PROJECT_DIR", raising=False)
        monkeypatch.setattr("kiro_crew.platform.update_layout.distribution", lambda: "docker")

        from kiro_crew.platform.update_layout import detect_install_layout

        layout = detect_install_layout()
        assert layout.kind == "docker"
        assert layout.is_externally_managed is True
        assert "docker pull" in layout.guidance.lower()

    def test_source_distribution_treated_as_wheel(self, monkeypatch) -> None:
        """Unstamped builds (no _build_info) report 'source' — still feed-checkable."""
        monkeypatch.delenv("KIROCREW_PROJECT_DIR", raising=False)
        monkeypatch.setattr("kiro_crew.platform.update_layout.distribution", lambda: "source")

        from kiro_crew.platform.update_layout import detect_install_layout

        layout = detect_install_layout()
        assert layout.kind == "source"
        assert layout.is_git is False
        assert layout.is_externally_managed is False


class TestReleaseChannel:
    """Tests for platform/update_layout.release_channel.

    The seam is ``data_home``, not ``config_dir``: ``release_channel`` is reached
    from the async update check, and ``config_dir`` is resolve-AND-maintain (it
    refreshes the recovery breadcrumb and re-runs a leftover-archive sweep that can
    ``shutil.rmtree``), so calling it there put a destructive sweep on the event
    loop -- issue #1057. ``test_no_config_dir_in_async.py`` guards the production
    side; patch whichever name that module actually uses.
    """

    def test_reads_channel_file(self, monkeypatch, tmp_path) -> None:
        (tmp_path / "channel").write_text("insider\n")
        monkeypatch.setattr("kiro_crew.platform.update_layout.data_home", lambda: tmp_path)

        from kiro_crew.platform.update_layout import release_channel

        assert release_channel() == "insider"

    def test_defaults_to_stable(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setattr("kiro_crew.platform.update_layout.data_home", lambda: tmp_path)

        from kiro_crew.platform.update_layout import release_channel

        assert release_channel() == "stable"

    def test_invalid_channel_falls_to_stable(self, monkeypatch, tmp_path) -> None:
        (tmp_path / "channel").write_text("bogus-channel\n")
        monkeypatch.setattr("kiro_crew.platform.update_layout.data_home", lambda: tmp_path)

        from kiro_crew.platform.update_layout import release_channel

        assert release_channel() == "stable"


class TestWheelUpdateCommand:
    """Tests for platform/update_layout.wheel_update_command."""

    def test_includes_channel(self, monkeypatch, tmp_path) -> None:
        (tmp_path / "channel").write_text("nightly\n")
        monkeypatch.setattr("kiro_crew.platform.update_layout.data_home", lambda: tmp_path)
        monkeypatch.delenv("KIROCREW_CDN_BASE", raising=False)

        from kiro_crew.platform.update_layout import wheel_update_command

        cmd = wheel_update_command("nightly")
        assert "--channel nightly" in cmd
        assert "https://download.crew.kiro.dev/cli.sh" in cmd
        assert "--proto '=https'" in cmd

    def test_cdn_override(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("KIROCREW_CDN_BASE", "https://custom.cdn.example")
        monkeypatch.setattr("kiro_crew.platform.update_layout.data_home", lambda: tmp_path)

        from kiro_crew.platform.update_layout import wheel_update_command

        cmd = wheel_update_command("stable")
        assert "https://custom.cdn.example/cli.sh" in cmd


class TestUpdateWheelCli:
    """Integration tests for ``_update_wheel`` in cli_server.py."""

    def _make_manifest(self, version: str = "0.2.0", channel: str = "stable") -> bytes:
        return json.dumps(
            {
                "schema": "kirocrew-cli-artifact-manifest-v1",
                "channel": channel,
                "version": version,
                "pub_date": "2026-08-06T12:00:00Z",
            }
        ).encode("utf-8")

    def test_up_to_date_exits_cleanly(self, monkeypatch, tmp_path, capsys) -> None:
        """When local == remote, prints 'already on latest' and returns."""
        monkeypatch.delenv("KIROCREW_PROJECT_DIR", raising=False)
        monkeypatch.setattr("kiro_crew.platform.update_layout.distribution", lambda: "wheel")
        monkeypatch.setattr("kiro_crew.platform.update_layout.data_home", lambda: tmp_path)
        monkeypatch.delenv("KIROCREW_CDN_BASE", raising=False)

        # Pretend local is 0.2.0 and feed also reports 0.2.0
        monkeypatch.setattr("kiro_crew.cli_server.__version__", "0.2.0")
        monkeypatch.setattr("kiro_crew.__version__", "0.2.0")

        import kiro_crew.cli_server as cs
        from kiro_crew.platform.update_layout import InstallLayout

        layout = InstallLayout(
            kind="wheel", proj="", is_git=False, is_externally_managed=False, guidance=""
        )

        # Mock urllib to return a matching version

        manifest = self._make_manifest("0.2.0")

        class FakeResp:
            def read(self, n=-1):
                return manifest

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

        monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: FakeResp())

        cs._update_wheel(layout)
        out = capsys.readouterr().out
        assert "latest version" in out.lower()

    def test_newer_version_runs_installer(self, monkeypatch, tmp_path, capsys) -> None:
        """When feed has a newer version, runs the shell installer."""
        monkeypatch.delenv("KIROCREW_PROJECT_DIR", raising=False)
        monkeypatch.setattr("kiro_crew.platform.update_layout.distribution", lambda: "wheel")
        monkeypatch.setattr("kiro_crew.platform.update_layout.data_home", lambda: tmp_path)
        monkeypatch.delenv("KIROCREW_CDN_BASE", raising=False)

        monkeypatch.setattr("kiro_crew.cli_server.__version__", "0.1.3")
        monkeypatch.setattr("kiro_crew.__version__", "0.1.3")

        import kiro_crew.cli_server as cs
        from kiro_crew.platform.update_layout import InstallLayout

        layout = InstallLayout(
            kind="wheel", proj="", is_git=False, is_externally_managed=False, guidance=""
        )

        manifest = self._make_manifest("0.2.0")

        class FakeResp:
            def read(self, n=-1):
                return manifest

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

        monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: FakeResp())

        # Mock subprocess.run to capture the installer invocation
        calls: list[tuple] = []

        def fake_run(*args, **kwargs):
            calls.append(args)
            result = MagicMock()
            result.returncode = 0
            return result

        monkeypatch.setattr("subprocess.run", fake_run)
        # Ensure the platform guard doesn't short-circuit on Windows CI.
        monkeypatch.setattr("sys.platform", "linux")

        cs._update_wheel(layout)
        out = capsys.readouterr().out
        assert "updated to 0.2.0" in out.lower()
        assert calls, "installer should have been invoked"
        # The installer should be run via sh -c
        assert calls[0][0][0] == "sh"
        assert calls[0][0][1] == "-c"

    def test_feed_unreachable_prints_manual_command(self, monkeypatch, tmp_path, capsys) -> None:
        """Network failure prints the manual update command."""
        monkeypatch.delenv("KIROCREW_PROJECT_DIR", raising=False)
        monkeypatch.setattr("kiro_crew.platform.update_layout.distribution", lambda: "wheel")
        monkeypatch.setattr("kiro_crew.platform.update_layout.data_home", lambda: tmp_path)
        monkeypatch.delenv("KIROCREW_CDN_BASE", raising=False)
        (tmp_path / "channel").write_text("stable\n")

        import urllib.error

        import kiro_crew.cli_server as cs
        from kiro_crew.platform.update_layout import InstallLayout

        layout = InstallLayout(
            kind="wheel", proj="", is_git=False, is_externally_managed=False, guidance=""
        )

        def raise_url_error(*a, **k):
            raise urllib.error.URLError("Network is down")

        monkeypatch.setattr("urllib.request.urlopen", raise_url_error)

        with pytest.raises(SystemExit) as exc_info:
            cs._update_wheel(layout)
        assert exc_info.value.code == 1
        out = capsys.readouterr().out
        assert "curl" in out  # shows manual command
        assert "cli.sh" in out

    def test_schema_mismatch_exits(self, monkeypatch, tmp_path, capsys) -> None:
        """Feed with wrong schema prints guidance and exits."""
        monkeypatch.delenv("KIROCREW_PROJECT_DIR", raising=False)
        monkeypatch.setattr("kiro_crew.platform.update_layout.distribution", lambda: "wheel")
        monkeypatch.setattr("kiro_crew.platform.update_layout.data_home", lambda: tmp_path)
        monkeypatch.delenv("KIROCREW_CDN_BASE", raising=False)

        import kiro_crew.cli_server as cs
        from kiro_crew.platform.update_layout import InstallLayout

        layout = InstallLayout(
            kind="wheel", proj="", is_git=False, is_externally_managed=False, guidance=""
        )

        bad_manifest = json.dumps({"schema": "wrong", "version": "1.0.0"}).encode()

        class FakeResp:
            def read(self, n=-1):
                return bad_manifest

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

        monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: FakeResp())

        with pytest.raises(SystemExit) as exc_info:
            cs._update_wheel(layout)
        assert exc_info.value.code == 1


class TestUpdateDispatch:
    """Tests for the top-level _update() dispatch in cli_server.py."""

    def test_externally_managed_prints_guidance(self, monkeypatch, capsys) -> None:
        """Desktop/Docker installs get guidance, not an error."""
        monkeypatch.delenv("KIROCREW_PROJECT_DIR", raising=False)
        # Both readers are bound at import, so patching `beacon.distribution`
        # alone would not reach them: the capability module decides the branch and
        # cli_server only names the stamp in the message.
        monkeypatch.setattr("kiro_crew.platform.update_capability.distribution", lambda: "dmg")
        monkeypatch.setattr("kiro_crew.cli_server.distribution", lambda: "dmg")

        import kiro_crew.cli_server as cs

        cs._update()
        out = capsys.readouterr().out
        assert "externally" in out.lower() or "desktop" in out.lower()

    def test_an_externally_managed_stamp_wins_over_a_mounted_checkout(
        self, monkeypatch, tmp_path, capsys
    ) -> None:
        """A container pointed at a real checkout must NOT take the git path.

        The git path is fetch + reset, so treating the mount as this install's
        source discards whatever the operator has in that tree — and the image,
        not the checkout, is what updates a container.
        """
        _init_repo(tmp_path)
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path))
        monkeypatch.setattr("kiro_crew.platform.update_capability.distribution", lambda: "docker")
        monkeypatch.setattr("kiro_crew.cli_server.distribution", lambda: "docker")

        def _no_git(*_a, **_k):  # pragma: no cover - must not be called
            raise AssertionError("the git update path must not run for a container")

        monkeypatch.setattr("subprocess.run", _no_git)

        import kiro_crew.cli_server as cs

        cs._update()
        out = capsys.readouterr().out.lower()
        assert "managed externally" in out
        assert "image" in out

    def test_git_checkout_still_works(self, monkeypatch, tmp_path) -> None:
        """Git installs still take the existing git fetch+reset path."""
        proj = tmp_path / "project"
        proj.mkdir()
        _init_repo(proj)
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(proj))
        monkeypatch.setattr(
            "kiro_crew.platform.update_capability.running_from_checkout",
            lambda root, **kw: True,
        )

        import kiro_crew.cli_server as cs

        # Stub out git subprocess calls to verify we reach the git path
        calls: list[list[str]] = []

        def fake_run(args, **kwargs):
            calls.append(args)
            result = MagicMock()
            result.returncode = 0
            # The detection asks for the working tree's own root and compares it
            # to the install root, so the fake has to answer with that root
            # rather than a branch name.
            result.stdout = f"{proj}\n" if "--show-toplevel" in args else "main\n"
            return result

        monkeypatch.setattr("subprocess.run", fake_run)
        monkeypatch.setattr(
            "kiro_crew.platform.update_governance.resolve_remote_url", lambda *a, **k: ""
        )
        monkeypatch.setattr(
            "kiro_crew.platform.update_governance.update_blocked_reason", lambda *a: ""
        )

        # The function will call git rev-parse, then git fetch, then git diff.
        # After git diff returns 0 (no new commits), it prints "Already up to date!"
        cs._update()
        # Verify git commands were issued
        git_calls = [c for c in calls if c[0] == "git"]
        assert any("rev-parse" in c for c in git_calls)
