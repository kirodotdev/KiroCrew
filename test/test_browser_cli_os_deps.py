"""Which Linux hosts ``--with-deps`` is offered on, and what the others are told."""

from __future__ import annotations

import platform
import shutil

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


def _managers(monkeypatch: pytest.MonkeyPatch, present: frozenset[str] | set[str]) -> None:
    """Make ``shutil.which`` find exactly *present* and nothing else.

    Stubbed for the same reason the os-release reader is: the test host has its
    own package managers (this repo's CI runs on hosts that carry ``dnf``), and
    an unstubbed probe would make these tests assert the CI image, not the code.
    ``sudo`` belongs in *present* only for tests modeling a sudo-capable host.
    """
    monkeypatch.setattr(
        shutil, "which", lambda name: f"/usr/bin/{name}" if name in present else None
    )


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
    def test_a_dnf_host_is_told_dnf_with_rpm_package_names(self, monkeypatch):
        _os_release(monkeypatch, {"ID": "fedora"})
        _managers(monkeypatch, {"dnf", "yum", "sudo"})
        command = mod.manual_deps_command()
        assert command is not None
        # The manager token itself, not merely "some string came back": a test
        # that only checks non-emptiness passes for the wrong reason, which is
        # how a hardcoded manager survived on hosts that do not have it.
        assert command.startswith("sudo dnf install -y ")
        # rpm names, not a mechanical mapping of Playwright's Debian list: a
        # command that fails on its own first package teaches the operator that
        # the remedy is broken.
        assert "mesa-libgbm" in command
        assert "cups-libs" in command
        assert "libgbm1" not in command
        assert "libcups2" not in command

    def test_a_yum_only_host_is_told_yum_not_dnf(self, monkeypatch):
        """Amazon Linux 2 and 7-era RHEL/CentOS carry ``yum`` and no ``dnf``.

        Both managers take the same rpm package names, so only the manager token
        differs; the hardcoded ``dnf`` was the whole defect on these hosts.
        """
        _os_release(monkeypatch, {"ID": "amzn", "VERSION_ID": "2"})
        _managers(monkeypatch, {"yum", "sudo"})
        command = mod.manual_deps_command()
        assert command is not None
        assert command.startswith("sudo yum install -y ")
        assert "dnf" not in command

    def test_dnf_wins_when_both_are_present(self, monkeypatch):
        """Modern rpm hosts ship ``yum`` as a compatibility shim for ``dnf``;
        the real manager is the one to name."""
        _os_release(monkeypatch, {"ID": "amzn", "ID_LIKE": "fedora", "VERSION_ID": "2023"})
        _managers(monkeypatch, {"dnf", "yum", "sudo"})
        command = mod.manual_deps_command()
        assert command is not None
        assert command.startswith("sudo dnf install -y ")

    def test_the_package_list_is_identical_for_dnf_and_yum(self, monkeypatch):
        _os_release(monkeypatch, {"ID": "centos", "ID_LIKE": "rhel fedora"})
        _managers(monkeypatch, {"dnf", "sudo"})
        dnf_command = mod.manual_deps_command()
        mod.linux_family.cache_clear()
        _managers(monkeypatch, {"yum", "sudo"})
        yum_command = mod.manual_deps_command()
        assert dnf_command is not None and yum_command is not None
        assert dnf_command.removeprefix("sudo dnf") == yum_command.removeprefix("sudo yum")

    def test_an_rpm_host_with_neither_manager_gets_silence_not_a_guess(self, monkeypatch):
        """An rpm host with no supported manager on PATH is offered nothing:
        silence is the rule the docstring already prescribes for a host we
        cannot serve correctly."""
        _os_release(monkeypatch, {"ID": "centos", "ID_LIKE": "rhel fedora"})
        _managers(monkeypatch, set())
        assert mod.linux_family() == mod.FAMILY_RPM
        assert mod.manual_deps_command() is None
        assert mod.missing_deps_hint() == ""

    @pytest.mark.parametrize(
        "present",
        [{"dnf"}, {"yum"}, {"dnf", "yum"}, set()],
        ids=["dnf-installed", "yum-installed", "both-installed", "neither"],
    )
    def test_a_suse_host_is_silent_no_matter_which_binaries_it_carries(self, monkeypatch, present):
        """SUSE resolves to the rpm family but is decided by LINEAGE, not by
        which binaries happen to be installed. ``dnf`` and ``yum`` are packaged
        in SUSE's own repos, where this module's Fedora/RHEL package names do
        not resolve (``libgbm1`` for ``mesa-libgbm``) -- a command completed for
        them would fail on its package list instead of its first argument, the
        same defect this probe exists to remove, wearing a different hat.
        """
        _os_release(monkeypatch, {"ID": "opensuse-leap", "ID_LIKE": "suse opensuse"})
        _managers(monkeypatch, present)
        assert mod.linux_family() == mod.FAMILY_RPM
        assert mod.manual_deps_command() is None
        assert mod.missing_deps_hint() == ""

    def test_a_microdnf_only_host_without_sudo_gets_a_bare_command(self, monkeypatch):
        """Minimal RHEL/UBI images ship ``microdnf`` and run as root, but do
        not carry ``sudo``. Their remedy must start with the manager that exists.
        """
        _os_release(monkeypatch, {"ID": "rhel", "ID_LIKE": "fedora"})
        _managers(monkeypatch, {"microdnf"})
        command = mod.manual_deps_command()
        assert command is not None
        assert command.startswith("microdnf install -y ")
        assert "sudo" not in command

    def test_apt_family_with_sudo_defers_to_playwright(self, monkeypatch):
        """On apt Playwright installs its own per-version set; a copy here goes
        stale against the CLI the user actually has."""
        _os_release(monkeypatch, {"ID": "ubuntu"})
        _managers(monkeypatch, {"sudo"})
        command = mod.manual_deps_command()
        assert command == "sudo npx playwright install-deps chromium"

    def test_apt_family_without_sudo_gets_a_bare_command(self, monkeypatch):
        _os_release(monkeypatch, {"ID": "debian"})
        _managers(monkeypatch, set())
        command = mod.manual_deps_command()
        assert command == "npx playwright install-deps chromium"
        assert "sudo" not in command

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
        _managers(monkeypatch, {"dnf", "sudo"})
        command = mod.manual_deps_command()
        hint = mod.missing_deps_hint()
        assert command is not None
        assert "root" in hint
        assert command in hint

    def test_nothing_here_runs_a_package_manager(self, monkeypatch):
        """This module composes a command for a human; it never elevates itself.

        Probing for the manager binary (``shutil.which``) is a directory stat,
        not a spawn. The guard patches every subprocess entry point a later
        edit would plausibly reach for, so a probe rewritten to shell out fails
        here instead of shipping.
        """
        _os_release(monkeypatch, {"ID": "amzn", "ID_LIKE": "fedora"})
        _managers(monkeypatch, {"dnf", "yum", "sudo"})
        import os
        import subprocess

        def _fail(*a, **k):
            pytest.fail("os_deps must not spawn")

        monkeypatch.setattr(subprocess, "run", _fail)
        monkeypatch.setattr(subprocess, "Popen", _fail)
        monkeypatch.setattr(subprocess, "check_output", _fail)
        monkeypatch.setattr(subprocess, "check_call", _fail)
        monkeypatch.setattr(os, "system", _fail)
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
