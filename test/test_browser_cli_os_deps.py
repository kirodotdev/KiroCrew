"""Which Linux hosts ``--with-deps`` is offered on, and what the others are told."""

from __future__ import annotations

import platform

import pytest

from kiro_crew import platform_compat
from kiro_crew.browser_cli import os_deps as mod


@pytest.fixture(autouse=True)
def _clear_family_cache():
    """``linux_family`` memoizes a value that cannot change under a real process."""
    mod.linux_family.cache_clear()
    yield
    mod.linux_family.cache_clear()


def _os_release(monkeypatch: pytest.MonkeyPatch, fields: dict[str, str]) -> None:
    """Make the host report *fields* as its freedesktop os-release.

    The stdlib reader is stubbed rather than a temp file written, because
    :func:`platform.freedesktop_os_release` memoizes its own result -- a real file
    would be read once and then shadow every later test in the worker.
    """
    monkeypatch.setattr(platform, "freedesktop_os_release", lambda: dict(fields))
    monkeypatch.setattr(platform_compat, "IS_LINUX", True)


def _no_os_release(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the host report no os-release at all, the way the stdlib does."""

    def _raise() -> dict[str, str]:
        raise OSError("no os-release on this host")

    monkeypatch.setattr(platform, "freedesktop_os_release", _raise)
    monkeypatch.setattr(platform_compat, "IS_LINUX", True)


class TestFamilyDetection:
    @pytest.mark.parametrize(
        ("fields", "expected"),
        [
            ({"ID": "ubuntu", "ID_LIKE": "debian"}, mod.FAMILY_DEBIAN),
            ({"ID": "debian"}, mod.FAMILY_DEBIAN),
            # A derivative names itself in ID and its base only in ID_LIKE.
            ({"ID": "linuxmint", "ID_LIKE": "ubuntu debian"}, mod.FAMILY_DEBIAN),
            # Amazon Linux 2023: the host this whole module exists for.
            ({"ID": "amzn", "ID_LIKE": "fedora", "VERSION_ID": "2023"}, mod.FAMILY_RPM),
            # Amazon Linux 2 omits ID_LIKE, so ID alone must resolve it.
            ({"ID": "amzn", "VERSION_ID": "2"}, mod.FAMILY_RPM),
            ({"ID": "centos", "ID_LIKE": "rhel fedora"}, mod.FAMILY_RPM),
            ({"ID": "fedora"}, mod.FAMILY_RPM),
            ({"ID": "alpine"}, mod.FAMILY_UNKNOWN),
            ({"PRETTY_NAME": "something"}, mod.FAMILY_UNKNOWN),
            # Extra whitespace in the ID_LIKE list must still tokenize.
            ({"ID": "pop", "ID_LIKE": "  ubuntu   debian "}, mod.FAMILY_DEBIAN),
            # The spec says lowercase; reality varies, so it is normalized.
            ({"ID": "Fedora"}, mod.FAMILY_RPM),
        ],
    )
    def test_it_reads_id_and_id_like(self, monkeypatch, fields, expected):
        _os_release(monkeypatch, fields)
        assert mod.linux_family() == expected

    def test_an_absent_os_release_is_unknown_rather_than_a_guess(self, monkeypatch):
        _no_os_release(monkeypatch)
        assert mod.linux_family() == mod.FAMILY_UNKNOWN

    def test_non_linux_never_reads_the_file(self, monkeypatch):
        """macOS and Windows have no OS-package step, so the read is skipped."""
        monkeypatch.setattr(platform_compat, "IS_LINUX", False)
        monkeypatch.setattr(
            mod, "_os_release_ids", lambda: pytest.fail("must not read os-release off Linux")
        )
        assert mod.linux_family() == mod.FAMILY_UNKNOWN


class TestWithDepsIsOfferedOnlyWherePlaywrightHonoursIt:
    def test_apt_family_gets_the_flag(self, monkeypatch):
        _os_release(monkeypatch, {"ID": "ubuntu"})
        assert mod.with_deps_supported() is True

    def test_rpm_family_does_not(self, monkeypatch):
        """Playwright has no rpm path: it picks Ubuntu package names and runs
        ``apt-get`` anyway, which fails on both the names and the privilege -- and
        because the flag and the download are one invocation, takes the download
        with it."""
        _os_release(monkeypatch, {"ID": "amzn", "ID_LIKE": "fedora"})
        assert mod.with_deps_supported() is False

    def test_unknown_linux_does_not(self, monkeypatch):
        _os_release(monkeypatch, {"ID": "alpine"})
        assert mod.with_deps_supported() is False

    def test_non_linux_does_not(self, monkeypatch):
        monkeypatch.setattr(platform_compat, "IS_LINUX", False)
        assert mod.with_deps_supported() is False


class TestTheManualRemedy:
    def test_rpm_family_names_dnf_and_rpm_package_names(self, monkeypatch):
        _os_release(monkeypatch, {"ID": "amzn", "ID_LIKE": "fedora"})
        command = mod.manual_deps_command()
        assert command is not None
        assert command.startswith("sudo dnf install -y ")
        # rpm names, not a mechanical mapping of Playwright's Debian list: a
        # command that fails on its own first package teaches the operator that
        # the remedy is broken.
        assert "mesa-libgbm" in command
        assert "cups-libs" in command
        assert "libgbm1" not in command
        assert "libcups2" not in command

    def test_apt_family_defers_to_playwright_rather_than_pinning_a_list(self, monkeypatch):
        """On apt Playwright installs its own per-version set; a copy here goes
        stale against the CLI the user actually has."""
        _os_release(monkeypatch, {"ID": "ubuntu"})
        command = mod.manual_deps_command()
        assert command == "sudo npx playwright install-deps chromium"

    def test_unknown_linux_offers_nothing(self, monkeypatch):
        _os_release(monkeypatch, {"ID": "alpine"})
        assert mod.manual_deps_command() is None
        assert mod.missing_deps_hint() == ""

    def test_non_linux_offers_nothing(self, monkeypatch):
        monkeypatch.setattr(platform_compat, "IS_LINUX", False)
        assert mod.manual_deps_command() is None
        assert mod.missing_deps_hint() == ""

    def test_the_hint_carries_the_command_and_says_root_is_needed(self, monkeypatch):
        _os_release(monkeypatch, {"ID": "amzn", "ID_LIKE": "fedora"})
        command = mod.manual_deps_command()
        hint = mod.missing_deps_hint()
        assert command is not None
        assert "root" in hint
        assert command in hint

    def test_nothing_here_runs_a_package_manager(self, monkeypatch):
        """This module composes a command for a human; it never elevates itself."""
        _os_release(monkeypatch, {"ID": "amzn", "ID_LIKE": "fedora"})
        import subprocess

        monkeypatch.setattr(
            subprocess, "run", lambda *a, **k: pytest.fail("os_deps must not spawn")
        )
        mod.linux_family()
        mod.with_deps_supported()
        mod.manual_deps_command()
        mod.missing_deps_hint()


class TestTheHostValidationWarningIsAFailure:
    """Playwright reports a browser that cannot launch as a WARNING, and exits 0.

    MEASURED on Amazon Linux 2023: with libraries missing, ``install-browser``
    prints the box below and exits 0. Reading the exit code alone reports the
    install as green and defers the real error to the user's first browse.
    """

    #: The real output, trimmed. Kept verbatim so a reworded box is caught here.
    _REAL = (
        "Playwright Host validation warning: \n"
        "╔══════════════════════════════════════════════════════╗\n"
        "║ Host system is missing dependencies to run browsers. ║\n"
        "║ Missing libraries:                                   ║\n"
        "║     libgtk-4.so.1                                    ║\n"
        "╚══════════════════════════════════════════════════════╝\n"
        "    at validateDependenciesLinux (/n/coreBundle.js:32000:9)\n"
    )

    def test_the_real_output_is_detected(self):
        assert mod.host_deps_unsatisfied(self._REAL) is True

    @pytest.mark.parametrize(
        "text",
        [
            "Playwright Host validation warning:",
            "HOST SYSTEM IS MISSING DEPENDENCIES TO RUN BROWSERS.",
            "host validation warning",
        ],
        ids=["header-only", "message-shouted", "already-lowercase"],
    )
    def test_either_marker_alone_is_enough_and_case_does_not_matter(self, text):
        """Header and message come from different call sites, so one reworded box
        still trips the other."""
        assert mod.host_deps_unsatisfied(text) is True

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "Downloading Chromium 141.0 (playwright build v1237)",
            "chromium 141.0 downloaded to /home/u/.cache/ms-playwright/chromium-1237",
            "npm warn deprecated foo@1.0.0",
        ],
        ids=["empty", "progress", "success", "npm-noise"],
    )
    def test_ordinary_output_is_not_a_failure(self, text):
        """A false positive here fails an install that actually worked."""
        assert mod.host_deps_unsatisfied(text) is False

    def test_none_is_tolerated(self):
        assert mod.host_deps_unsatisfied(None) is False  # type: ignore[arg-type]
