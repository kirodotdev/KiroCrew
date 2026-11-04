"""Which Linux hosts ``--with-deps`` is offered on, and what the others are told."""

from __future__ import annotations

import os
import platform
import shutil
import struct
from pathlib import Path

import pytest

from kiro_crew import platform_compat
from kiro_crew.browser_cli import os_deps as mod

ELF_MAGIC = b"\x7fELF"


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


class TestTheRemedyIsEngineAware:
    def test_apt_family_names_the_engine_it_was_asked_for(self, monkeypatch):
        _os_release(monkeypatch, {"ID": "ubuntu"})
        _managers(monkeypatch, {"sudo"})
        assert mod.manual_deps_command("firefox") == "sudo npx playwright install-deps firefox"
        hint = mod.missing_deps_hint("webkit")
        assert "WebKit" in hint
        assert "install-deps webkit" in hint

    def test_rpm_family_offers_its_package_list_for_chromium_only(self, monkeypatch):
        _os_release(monkeypatch, {"ID": "fedora"})
        _managers(monkeypatch, {"dnf", "sudo"})
        assert mod.manual_deps_command("chromium") is not None
        for engine, title in (("firefox", "Firefox"), ("webkit", "WebKit")):
            assert mod.manual_deps_command(engine) is None
            hint = mod.missing_deps_hint(engine)
            assert title in hint
            # Fedora was not measured, so both unlisted engines keep the manual
            # instruction there; the WebKit verdict is Amazon Linux's alone.
            assert "no verified package list" in hint
            # Chromium's package set must not be offered as the fix.
            assert "mesa-libgbm" not in hint
            assert "dnf install" not in hint

    def test_the_chromium_hint_names_chromium(self, monkeypatch):
        _os_release(monkeypatch, {"ID": "fedora"})
        _managers(monkeypatch, {"dnf", "sudo"})
        assert mod.missing_deps_hint("chromium").startswith("The Chromium browser")

    def test_unknown_linux_stays_silent_for_every_engine(self, monkeypatch):
        _os_release(monkeypatch, {"ID": "alpine"})
        for engine in ("chromium", "firefox", "webkit"):
            assert mod.missing_deps_hint(engine) == ""


AMAZON_LINUX_2023 = {"ID": "amzn", "ID_LIKE": "fedora", "VERSION_ID": "2023"}

#: rpm-family hosts no capture was taken on. Fedora, for one, packages several of
#: the libraries Amazon Linux lacks, so an operator there can follow the manual
#: instruction; the verdict would tell that operator to stop trying.
UNMEASURED_RPM_HOSTS = [
    {"ID": "fedora"},
    {"ID": "rhel", "ID_LIKE": "fedora"},
    {"ID": "centos", "ID_LIKE": "rhel fedora"},
    {"ID": "rocky", "ID_LIKE": "rhel centos fedora"},
    {"ID": "almalinux", "ID_LIKE": "rhel centos fedora"},
    {"ID": "opensuse-leap", "ID_LIKE": "suse opensuse"},
]


class TestTheUnsupportedEngineVerdict:
    """What a host measured not to run an engine is told, and who is not.

    MEASURED on Amazon Linux 2023 (os-release ``ID=amzn``): ``install-browser
    webkit`` names 27 missing libraries, most with no package in the
    distribution's repositories, while ``install-browser firefox`` on the same
    host passes host validation. So the verdict is WebKit's, on that
    distribution; Firefox keeps the manual instruction, and so does WebKit on
    every rpm distribution no capture was taken on.
    """

    def test_webkit_on_amazon_linux_is_unsupported_not_a_library_list(self, monkeypatch):
        _os_release(monkeypatch, AMAZON_LINUX_2023)
        _managers(monkeypatch, {"dnf", "sudo"})
        assert mod.engine_unsupported_here("webkit") is True
        hint = mod.missing_deps_hint("webkit")
        assert hint.startswith("WebKit is not supported on this operating system")
        assert "Chromium is the supported browser engine" in hint
        # Neither a package list nor a retry: there is nothing the operator can run.
        assert "install -y" not in hint
        assert "retry" not in hint
        assert "no verified package list" not in hint

    def test_the_verdict_composes_no_command_so_it_needs_no_package_manager(self, monkeypatch):
        _os_release(monkeypatch, AMAZON_LINUX_2023)
        _managers(monkeypatch, set())
        assert mod.manual_deps_command("chromium") is None
        assert mod.missing_deps_hint("webkit").startswith("WebKit is not supported")

    @pytest.mark.parametrize("fields", UNMEASURED_RPM_HOSTS, ids=lambda f: f["ID"])
    def test_every_other_rpm_distribution_keeps_the_manual_instruction(self, monkeypatch, fields):
        """One host's measurement is that host's verdict. Elsewhere in the family
        the libraries may well be installable, so the operator is still told how."""
        _os_release(monkeypatch, fields)
        _managers(monkeypatch, {"dnf", "sudo"})
        assert mod.linux_family() == mod.FAMILY_RPM
        assert mod.engine_unsupported_here("webkit") is False
        hint = mod.missing_deps_hint("webkit")
        assert hint.startswith("The WebKit browser needs OS libraries")
        assert "no verified package list for WebKit" in hint
        assert "then retry the download" in hint
        assert "not supported" not in hint

    def test_the_verdict_is_keyed_on_the_distribution_id_not_its_lineage(self, monkeypatch):
        """``amzn`` reports ``ID_LIKE=fedora``; that lineage must not carry the
        verdict to Fedora, and a derivative naming ``amzn`` only in ``ID_LIKE``
        was not measured either."""
        _os_release(monkeypatch, {"ID": "fedora", "ID_LIKE": "amzn"})
        _managers(monkeypatch, {"dnf", "sudo"})
        assert mod.engine_unsupported_here("webkit") is False
        assert "no verified package list for WebKit" in mod.missing_deps_hint("webkit")

    @pytest.mark.parametrize(
        "fields", [AMAZON_LINUX_2023, {"ID": "fedora"}], ids=["amzn", "fedora"]
    )
    def test_firefox_keeps_the_manual_instruction(self, monkeypatch, fields):
        """Firefox's build passes host validation on Amazon Linux 2023, so a
        missing library there is one the operator can install by name."""
        _os_release(monkeypatch, fields)
        _managers(monkeypatch, {"dnf", "sudo"})
        assert mod.engine_unsupported_here("firefox") is False
        hint = mod.missing_deps_hint("firefox")
        assert hint.startswith("The Firefox browser needs OS libraries")
        assert "no verified package list for Firefox" in hint
        assert "not supported" not in hint

    @pytest.mark.parametrize(
        "fields", [AMAZON_LINUX_2023, {"ID": "fedora"}], ids=["amzn", "fedora"]
    )
    def test_chromium_keeps_its_remedy(self, monkeypatch, fields):
        """Chromium has a verified package list, so its failure stays a remedy."""
        _os_release(monkeypatch, fields)
        _managers(monkeypatch, {"dnf", "sudo"})
        hint = mod.missing_deps_hint("chromium")
        assert hint.startswith("The Chromium browser")
        assert "sudo dnf install -y " in hint

    def test_apt_family_offers_install_deps_instead(self, monkeypatch):
        """Playwright's own installer serves every engine there."""
        _os_release(monkeypatch, {"ID": "ubuntu"})
        _managers(monkeypatch, {"sudo"})
        hint = mod.missing_deps_hint("webkit")
        assert "install-deps webkit" in hint
        assert "not supported" not in hint

    def test_unknown_linux_and_non_linux_say_nothing(self, monkeypatch):
        _os_release(monkeypatch, {"ID": "alpine"})
        assert mod.missing_deps_hint("webkit") == ""
        mod.linux_family.cache_clear()
        monkeypatch.setattr(platform_compat, "IS_LINUX", False)
        assert mod.engine_unsupported_here("webkit") is False
        assert mod.missing_deps_hint("webkit") == ""


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
        mod.missing_deps_hint("firefox")


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


class TestMissingSharedLibrariesDoesNotTrustPlaywright:
    """The probe reads files as DATA. Nothing in this class spawns a process."""

    @staticmethod
    def _elf(path: Path, needed: list[str]) -> None:
        """Write a minimal but REAL ELF64 little-endian file declaring *needed*.

        Hand-built rather than mocked: the parser's whole job is to read a real
        dynamic table, so a fake would test nothing about it.
        """
        names = b"\x00" + b"\x00".join(n.encode() for n in needed) + b"\x00"
        offsets, cursor = [], 1
        for n in needed:
            offsets.append(cursor)
            cursor += len(n) + 1

        ph_off, ph_entry = 64, 56
        dyn_off = ph_off + 2 * ph_entry
        entries = [(1, o) for o in offsets] + [(5, 0x1000), (0, 0)]
        dyn = b"".join(struct.pack("<Qq", t, v) for t, v in entries)
        str_off = dyn_off + len(dyn)

        header = bytearray(64)
        header[0:4] = b"\x7fELF"
        header[4] = 2  # ELF64
        header[5] = 1  # little endian
        header[6] = 1  # version
        header[16:18] = struct.pack("<H", 2)  # e_type ET_EXEC
        header[18:20] = struct.pack("<H", 183)  # e_machine aarch64
        header[32:40] = struct.pack("<Q", ph_off)  # e_phoff
        header[54:56] = struct.pack("<H", ph_entry)  # e_phentsize
        header[56:58] = struct.pack("<H", 2)  # e_phnum

        # PT_LOAD mapping vaddr 0x1000 onto the string table's file offset.
        load = struct.pack("<IIQQQQQQ", 1, 4, str_off, 0x1000, 0x1000, len(names), len(names), 8)
        dynamic = struct.pack("<IIQQQQQQ", 2, 6, dyn_off, 0x2000, 0x2000, len(dyn), len(dyn), 8)
        path.write_bytes(bytes(header) + load + dynamic + dyn + names)
        path.chmod(0o755)

    def _browser_dir(self, tmp_path, needed=("libatk-1.0.so.0",), name="chrome-linux-arm64"):
        """A build laid out under a subdirectory Playwright's constant does NOT name."""
        d = tmp_path / "chromium-1243" / name
        d.mkdir(parents=True)
        self._elf(d / "chrome", list(needed))
        return tmp_path / "chromium-1243"

    def test_a_host_missing_soname_is_reported(self, monkeypatch, tmp_path):
        monkeypatch.setattr(platform_compat, "IS_LINUX", True)
        browser = self._browser_dir(tmp_path, needed=["libatk-1.0.so.0", "libgbm.so.1"])
        monkeypatch.setattr(mod, "_host_provided_sonames", lambda arch=None: {"libc.so.6"})

        assert mod.missing_shared_libraries([browser]) == {
            "libatk-1.0.so.0",
            "libgbm.so.1",
        }

    def test_a_resolvable_build_reports_empty_not_none(self, monkeypatch, tmp_path):
        """Empty and ``None`` are different answers and callers branch on both."""
        monkeypatch.setattr(platform_compat, "IS_LINUX", True)
        browser = self._browser_dir(tmp_path, needed=["libatk-1.0.so.0"])
        monkeypatch.setattr(mod, "_host_provided_sonames", lambda arch=None: {"libatk-1.0.so.0"})

        assert mod.missing_shared_libraries([browser]) == set()

    def test_a_bundled_soname_is_never_called_host_missing(self, monkeypatch, tmp_path):
        """A library the build carries is its own, even when the host lacks it."""
        monkeypatch.setattr(platform_compat, "IS_LINUX", True)
        root = tmp_path / "chromium-1243"
        build = root / "chrome-linux-arm64"
        build.mkdir(parents=True)
        self._elf(build / "chrome", ["libxul.so", "libatk-1.0.so.0"])
        (build / "libxul.so").write_bytes(b"\x7fELF")
        monkeypatch.setattr(mod, "_host_provided_sonames", lambda arch=None: set())

        assert mod.missing_shared_libraries([root]) == {"libatk-1.0.so.0"}

    def test_an_unparseable_build_answers_unknown_rather_than_clean(self, monkeypatch, tmp_path):
        """Nothing readable declaring a dependency is UNKNOWN, not a clean host."""
        monkeypatch.setattr(platform_compat, "IS_LINUX", True)
        root = tmp_path / "chromium-1243"
        build = root / "chrome-linux-arm64"
        build.mkdir(parents=True)
        junk = build / "chrome"
        junk.write_bytes(b"not an elf at all")
        junk.chmod(0o755)

        assert mod.missing_shared_libraries([root]) is None

    def test_off_linux_is_unknown(self, monkeypatch, tmp_path):
        monkeypatch.setattr(platform_compat, "IS_LINUX", False)
        assert mod.missing_shared_libraries([tmp_path]) is None

    def test_an_absent_directory_is_unknown(self, monkeypatch, tmp_path):
        monkeypatch.setattr(platform_compat, "IS_LINUX", True)
        assert mod.missing_shared_libraries([tmp_path / "nope"]) is None


class TestNeededSonamesNeverExecutesAndNeverRaises:
    """The parser faces files from a writable cache, so malformed is the norm."""

    def test_a_truncated_file_is_empty_not_an_error(self, tmp_path):
        p = tmp_path / "cut"
        p.write_bytes(b"\x7fELF\x02\x01\x01")
        assert mod._needed_sonames(p) == set()

    def test_a_non_elf_is_empty(self, tmp_path):
        p = tmp_path / "text"
        p.write_text("#!/bin/sh\necho hi\n")
        assert mod._needed_sonames(p) == set()

    def test_an_absent_file_is_empty(self, tmp_path):
        assert mod._needed_sonames(tmp_path / "gone") == set()

    def test_an_absurd_program_header_count_is_refused(self, tmp_path):
        p = tmp_path / "bogus"
        header = bytearray(64)
        header[0:4] = b"\x7fELF"
        header[4], header[5] = 2, 1
        header[32:40] = struct.pack("<Q", 64)
        header[54:56] = struct.pack("<H", 56)
        header[56:58] = struct.pack("<H", 60000)  # over the ceiling
        p.write_bytes(bytes(header))
        assert mod._needed_sonames(p) == set()

    def test_a_real_system_binary_declares_libc(self):
        """Proves the parser works on a genuine ELF, not only hand-built ones.

        Needs a host whose system binaries ARE ELF: on macOS the same paths exist
        and hold Mach-O, which this parser correctly reads as "not an ELF" -- so an
        unguarded version of this test asserts a Linux fact on a Darwin runner.
        """
        if not platform_compat.IS_LINUX:
            pytest.skip("system binaries are only ELF on Linux")
        for candidate in ("/bin/ls", "/usr/bin/ls", "/bin/sh"):
            path = Path(candidate)
            if path.exists() and not path.is_symlink():
                assert any(n.startswith("libc.so") for n in mod._needed_sonames(path))
                return
        pytest.skip("no unlinked system binary available to parse")


class TestSystemLibraryDirsIsPlatformScoped:
    """The POSIX literals live in platform_compat; this pins what it answers."""

    def test_off_linux_it_answers_nothing(self, monkeypatch):
        monkeypatch.setattr(platform_compat, "IS_LINUX", False)
        assert platform_compat.system_library_dirs() == ()

    def test_on_linux_it_includes_the_loader_defaults_without_duplicates(self):
        if not platform_compat.IS_LINUX:
            pytest.skip("Linux-only question")
        dirs = platform_compat.system_library_dirs()
        assert len(dirs) == len(set(dirs))
        assert Path("/lib64") in dirs or Path("/lib") in dirs

    def test_the_host_soname_scan_finds_libc(self):
        """Proves the scan answers the real question on a real host."""
        if not platform_compat.IS_LINUX:
            pytest.skip("Linux-only question")
        assert any(n.startswith("libc.so") for n in mod._host_provided_sonames())


class TestEngineBuildDirs:
    """Which cache roots the probe walks, and that finding them is bounded."""

    def test_the_headless_shell_is_covered_not_just_chromium(self, tmp_path):
        """Headless is the default launch mode, so the shell is what a browse
        starts; checking only ``chromium-<rev>`` would clear the wrong build."""
        for name in ("chromium-1243", "chromium_headless_shell-1243", "firefox-1542"):
            (tmp_path / name).mkdir()

        names = sorted(p.name for p in mod.engine_build_dirs(tmp_path, "chromium", [100]))

        assert names == ["chromium-1243", "chromium_headless_shell-1243"]

    def test_a_symlinked_root_is_refused(self, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        cache = tmp_path / "cache"
        cache.mkdir()
        try:
            (cache / "chromium-9999").symlink_to(outside, target_is_directory=True)
        except OSError:
            pytest.skip("symlinks unavailable")

        assert list(mod.engine_build_dirs(cache, "chromium", [100])) == []

    def test_padding_the_cache_root_spends_the_shared_budget(self, tmp_path, monkeypatch):
        """Every root entry is charged, matching or not, and a spent budget makes
        the whole probe answer "cannot determine"."""
        for n in range(30):
            (tmp_path / f"chromium_pad{n:03d}").mkdir()
        monkeypatch.setattr(platform_compat, "IS_LINUX", True)
        monkeypatch.setattr(mod, "_MAX_WALK_ENTRIES", 5)
        monkeypatch.setattr(mod, "_needed_sonames", lambda path: {"libgbm.so.1"})

        budget = [5]
        assert len(list(mod.engine_build_dirs(tmp_path, "chromium", budget))) == 5
        assert budget == [-1]
        assert mod.missing_engine_libraries(tmp_path, "chromium") is None

    def test_no_cache_or_no_linux_is_unknown(self, tmp_path, monkeypatch):
        assert mod.missing_engine_libraries(None, "chromium") is None
        monkeypatch.setattr(platform_compat, "IS_LINUX", False)
        assert mod.missing_engine_libraries(tmp_path, "chromium") is None

    def test_an_unreadable_cache_is_unknown(self, tmp_path, monkeypatch):
        monkeypatch.setattr(platform_compat, "IS_LINUX", True)
        assert mod.missing_engine_libraries(tmp_path / "absent", "chromium") is None

    def test_a_real_build_is_probed_through_the_cache(self, tmp_path, monkeypatch):
        build = tmp_path / "chromium-1243"
        build.mkdir()
        main = build / "chrome"
        main.write_bytes(ELF_MAGIC)
        main.chmod(0o755)
        monkeypatch.setattr(platform_compat, "IS_LINUX", True)
        monkeypatch.setattr(mod, "_needed_sonames", lambda path: {"libc.so.6", "libgbm.so.1"})
        monkeypatch.setattr(mod, "_host_provided_sonames", lambda: {"libc.so.6"})

        assert mod.missing_engine_libraries(tmp_path, "chromium") == {"libgbm.so.1"}


class TestTheParserNeverWaitsOnACacheEntry:
    """The cache is writable, so an entry checked as a file can be swapped."""

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX FIFOs only")
    def test_a_fifo_is_refused_without_waiting_for_a_writer(self, tmp_path):
        """A blocking open of a FIFO waits for a writer forever and would hold the
        install slot; the parser must refuse it at once instead."""
        fifo = tmp_path / "chrome"
        os.mkfifo(fifo)

        assert mod._needed_sonames(fifo) == set()

    def test_a_symlink_at_the_name_is_not_followed(self, tmp_path):
        real = tmp_path / "real-chrome"
        real.write_bytes(ELF_MAGIC)
        link = tmp_path / "chrome"
        try:
            link.symlink_to(real)
        except OSError:
            pytest.skip("symlinks unavailable")

        assert mod._needed_sonames(link) == set()


class TestTheWalkIsBoundedAgainstAHostileCache:
    """The cache is writable, so its breadth and shape are hostile input."""

    def test_a_symlink_is_not_followed_into(self, tmp_path):
        """A planted link must not aim the walk at a tree of its own choosing."""
        outside = tmp_path / "outside"
        outside.mkdir()
        planted = outside / "libevil.so"
        planted.write_bytes(b"\x7fELF")
        build = tmp_path / "chromium-1243"
        build.mkdir()
        (build / "reach").symlink_to(outside)

        found = mod._elf_candidates(build, [mod._MAX_WALK_ENTRIES])

        assert all("outside" not in str(p) for p in found)

    def test_the_walk_stops_at_the_entry_ceiling(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mod, "_MAX_WALK_ENTRIES", 5)
        build = tmp_path / "chromium-1243"
        build.mkdir()
        for index in range(40):
            member = build / f"lib{index:03d}.so"
            member.write_bytes(b"\x7fELF")

        assert len(mod._elf_candidates(build, [mod._MAX_WALK_ENTRIES])) <= 5

    def test_the_main_executable_survives_truncation(self, tmp_path, monkeypatch):
        """The order is what makes the ceiling safe, so it is asserted with it."""
        monkeypatch.setattr(mod, "_MAX_WALK_ENTRIES", 3)
        build = tmp_path / "chromium-1243"
        nested = build / "deep" / "deeper"
        nested.mkdir(parents=True)
        for index in range(10):
            (nested / f"lib{index:03d}.so").write_bytes(b"\x7fELF")
        main = build / "chrome"
        main.write_bytes(b"\x7fELF")
        main.chmod(0o755)

        found = mod._elf_candidates(build, [mod._MAX_WALK_ENTRIES])

        assert found and found[0] == main


class TestHostSonamesIgnoreEnvironmentPaths:
    """A directory named by process environment is not listed."""

    def test_ld_library_path_is_not_consulted(self, tmp_path, monkeypatch):
        """The loader honours it, and this deliberately does not: listing a
        directory chosen by environment during a privileged install is the class
        this repository fences. The cost is a spurious advisory line."""
        vendor = tmp_path / "vendor"
        vendor.mkdir()
        (vendor / "libcustom-vendor.so.7").write_bytes(ELF_MAGIC)
        monkeypatch.setattr(platform_compat, "system_library_dirs", lambda: ())
        monkeypatch.setenv("LD_LIBRARY_PATH", str(vendor))

        assert mod._host_provided_sonames() == set()


class TestTheBoundIsAppliedBeforeTheWork:
    """A ceiling checked after materialising the tree bounds nothing."""

    def test_the_walk_is_not_materialised_before_the_ceiling(self, tmp_path, monkeypatch):
        """The bug: `sorted(rglob(...))` consumes everything before any break."""
        build = tmp_path / "chromium-1243"
        build.mkdir()
        for n in range(40):
            (build / f"pad{n:03d}.so").write_bytes(ELF_MAGIC)
        monkeypatch.setattr(mod, "_MAX_WALK_ENTRIES", 5)

        # Exactly the ceiling, and it STOPPED rather than filtered: a
        # materialising walk would have touched all 40 before any check.
        assert len(list(mod._bounded_walk(build, [mod._MAX_WALK_ENTRIES]))) == 5

    def test_the_shipped_soname_walk_is_bounded_too(self, tmp_path, monkeypatch):
        """It shares the ceiling; an uncapped second walk voids the first."""
        build = tmp_path / "chromium-1243"
        build.mkdir()
        for n in range(40):
            (build / f"lib{n:03d}.so.1").write_bytes(ELF_MAGIC)
        monkeypatch.setattr(mod, "_MAX_WALK_ENTRIES", 5)

        assert len(mod._sonames_shipped_with_build([build], [mod._MAX_WALK_ENTRIES])) == 5

    def test_padding_files_consume_the_ceiling_for_the_soname_walk(self, tmp_path, monkeypatch):
        """The bug this pins: `rglob("*.so*")` counted only the matches, so a tree
        padded with non-library files was walked in full for free."""
        build = tmp_path / "chromium-1243"
        nested = build / "lib"
        nested.mkdir(parents=True)
        for n in range(30):
            (build / f"pad{n:03d}.txt").write_text("x")
        # One level down, not beside the padding: the walk is breadth-first, so
        # every top-level entry is read before this one whatever order the
        # filesystem lists them in.
        (nested / "libgbm.so.1").write_bytes(ELF_MAGIC)
        monkeypatch.setattr(mod, "_MAX_WALK_ENTRIES", 5)

        # The padding exhausts the ceiling before the library is reached, so the
        # walk stops rather than reporting a full result from a bounded look.
        assert mod._sonames_shipped_with_build([build], [mod._MAX_WALK_ENTRIES]) == set()

    def test_a_wide_directory_is_read_only_up_to_the_ceiling(self, tmp_path, monkeypatch):
        """The bound covers the directory READ, not only what is yielded:
        `Path.rglob` lists a whole directory before yielding its first entry."""
        build = tmp_path / "chromium-1243"
        build.mkdir()
        for n in range(40):
            (build / f"pad{n:03d}.so").write_bytes(ELF_MAGIC)
        pulled: list[str] = []
        real_scandir = os.scandir

        class _Counting:
            def __init__(self, path):
                self._it = real_scandir(path)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self._it.close()

            def __iter__(self):
                return self

            def __next__(self):
                # A real iterator, not a generator method: other callers of the
                # patched `os.scandir` (Windows `shutil.rmtree`) call next() on it.
                entry = next(self._it)
                pulled.append(entry.name)
                return entry

            def close(self):
                self._it.close()

        monkeypatch.setattr(mod.os, "scandir", _Counting)

        assert len(list(mod._bounded_walk(build, [5]))) == 5
        # One read past the ceiling at most: the entry that found it spent.
        assert len(pulled) <= 6

    def test_a_walk_that_ends_at_the_ceiling_is_not_marked_spent(self, tmp_path):
        build = tmp_path / "chromium-1243"
        build.mkdir()
        for n in range(3):
            (build / f"lib{n}.so").write_bytes(ELF_MAGIC)
        budget = [3]

        assert len(list(mod._bounded_walk(build, budget))) == 3
        assert budget == [0]

    def test_a_spent_budget_answers_unknown_not_clean(self, tmp_path, monkeypatch):
        """Decoys that sort ahead of the main binary can spend the parse budget,
        so the answer must be "cannot determine", never "nothing missing"."""
        build = tmp_path / "chromium-1243"
        build.mkdir()
        for n in range(6):
            decoy = build / f"a{n}"
            decoy.write_bytes(ELF_MAGIC)
            decoy.chmod(0o755)
        monkeypatch.setattr(platform_compat, "IS_LINUX", True)
        monkeypatch.setattr(mod, "_MAX_WALK_ENTRIES", 8)
        monkeypatch.setattr(mod, "_needed_sonames", lambda path: {"libc.so.6"})
        monkeypatch.setattr(mod, "_host_provided_sonames", lambda: {"libc.so.6"})

        assert mod.missing_shared_libraries([build]) is None

    def test_a_complete_look_still_answers(self, tmp_path, monkeypatch):
        build = tmp_path / "chromium-1243"
        build.mkdir()
        main = build / "chrome"
        main.write_bytes(ELF_MAGIC)
        main.chmod(0o755)
        monkeypatch.setattr(platform_compat, "IS_LINUX", True)
        monkeypatch.setattr(mod, "_needed_sonames", lambda path: {"libc.so.6", "libgbm.so.1"})
        monkeypatch.setattr(mod, "_host_provided_sonames", lambda: {"libc.so.6"})

        assert mod.missing_shared_libraries([build]) == {"libgbm.so.1"}

    def test_the_shallow_entries_are_the_ones_kept(self, tmp_path, monkeypatch):
        """rglob is top-down, so truncation keeps the main executable's level."""
        build = tmp_path / "chromium-1243"
        deep = build / "swiftshader" / "nested"
        deep.mkdir(parents=True)
        main = build / "chrome"
        main.write_bytes(ELF_MAGIC)
        main.chmod(0o755)
        for n in range(30):
            (deep / f"deep{n:03d}.so").write_bytes(ELF_MAGIC)
        monkeypatch.setattr(mod, "_MAX_WALK_ENTRIES", 4)

        assert main in mod._elf_candidates(build, [mod._MAX_WALK_ENTRIES])


class TestTheNameCountIsBounded:
    """Names come out of dynamic tables in a writable cache, so the COUNT is
    attacker-shaped just as the walk's breadth was."""

    @staticmethod
    def _elf_with_needed(path, count):
        """Write an ELF64 whose dynamic table declares *count* DT_NEEDED names."""
        names = [f"libpad{n:05d}.so.1".encode() for n in range(count)]
        strtab = b"\x00" + b"\x00".join(names) + b"\x00"
        offsets, at = [], 1
        for n in names:
            offsets.append(at)
            at += len(n) + 1
        dyn = b"".join(struct.pack("<qQ", 1, off) for off in offsets)
        dyn += struct.pack("<qQ", 5, 0) + struct.pack("<qQ", 0, 0)
        ph_off, dyn_off = 64, 64 + 56 * 2
        str_off = dyn_off + len(dyn)
        header = bytearray(64)
        header[0:4] = b"\x7fELF"
        header[4], header[5], header[6] = 2, 1, 1
        header[18:20] = struct.pack("<H", 183)
        header[32:40] = struct.pack("<Q", ph_off)
        header[54:56] = struct.pack("<H", 56)
        header[56:58] = struct.pack("<H", 2)
        load = struct.pack("<IIQQQQQQ", 1, 5, 0, 0, 0, str_off + len(strtab), 0, 0)
        dynamic = struct.pack("<IIQQQQQQ", 2, 4, dyn_off, dyn_off, 0, len(dyn), 0, 0)
        # DT_STRTAB vaddr must land inside the PT_LOAD above.
        dyn = dyn.replace(struct.pack("<qQ", 5, 0), struct.pack("<qQ", 5, str_off), 1)
        path.write_bytes(bytes(header) + load + dynamic + dyn + strtab)

    def test_one_file_cannot_exceed_the_cap(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mod, "_MAX_SONAMES", 10)
        target = tmp_path / "chrome"
        self._elf_with_needed(target, 40)

        assert len(mod._needed_sonames(target)) <= 10

    def test_the_aggregate_stops_at_the_cap(self, tmp_path, monkeypatch):
        build = tmp_path / "chromium-1243"
        build.mkdir()
        for n in range(6):
            f = build / f"bin{n}"
            self._elf_with_needed(f, 8)
            f.chmod(0o755)
        monkeypatch.setattr(mod, "_MAX_SONAMES", 10)
        monkeypatch.setattr(mod, "_host_provided_sonames", lambda: set())
        monkeypatch.setattr(mod, "_sonames_shipped_with_build", lambda dirs, budget: set())
        monkeypatch.setattr(platform_compat, "IS_LINUX", True)

        result = mod.missing_shared_libraries([build])

        # Truncated, and truncation only ever SHORTENS a report -- a name never
        # added cannot invent a missing library.
        assert result is not None
        assert 0 < len(result) < 48

    def test_the_budget_does_not_scale_with_the_number_of_directories(self, tmp_path, monkeypatch):
        """`engine_build_dirs` prefix-matches a writable cache, so the number of
        directories handed in is attacker-shaped too -- a per-directory ceiling is
        just that ceiling times N."""
        dirs = []
        for d in range(4):
            build = tmp_path / f"chromium-{d}"
            build.mkdir()
            for n in range(10):
                (build / f"pad{n}.so").write_bytes(ELF_MAGIC)
            dirs.append(build)
        monkeypatch.setattr(mod, "_MAX_WALK_ENTRIES", 12)
        monkeypatch.setattr(mod, "_host_provided_sonames", lambda: set())
        monkeypatch.setattr(platform_compat, "IS_LINUX", True)

        budget = [12]
        total = sum(len(mod._elf_candidates(d, budget)) for d in dirs)

        # One shared budget: the four walks together cannot exceed it, where four
        # independent ceilings would have allowed 48.
        assert total <= 12

    def test_parsing_is_charged_to_the_same_budget(self, tmp_path, monkeypatch):
        """A cache of files that each declare NOTHING never trips the name cap, so
        without charging parses the probe still opens and parses every one."""
        build = tmp_path / "chromium-1243"
        build.mkdir()
        for n in range(30):
            f = build / f"bin{n:03d}"
            f.write_bytes(ELF_MAGIC + b"\x00" * 60)  # parseable header, no DT_NEEDED
            f.chmod(0o755)
        monkeypatch.setattr(platform_compat, "IS_LINUX", True)
        monkeypatch.setattr(mod, "_host_provided_sonames", lambda: set())

        parsed: list = []
        real = mod._needed_sonames
        monkeypatch.setattr(mod, "_needed_sonames", lambda p: (parsed.append(p), real(p))[1])
        # MEASURED: the two walks over 30 files charge 30 each, so 70 leaves 10 for
        # parsing and 100 leaves enough for all 30.
        monkeypatch.setattr(mod, "_MAX_WALK_ENTRIES", 70)

        mod.missing_shared_libraries([build])

        # Parsing stops with the budget, well short of all 30 files -- and a cache
        # that spends the budget answers "cannot determine" rather than "clean",
        # which is the safe direction.
        assert 0 < len(parsed) <= 10

    def test_duplicate_and_excess_offsets_are_not_each_read(self, tmp_path, monkeypatch):
        """A table whose entries all point at one string must not cost one read per
        entry: distinct offsets are read once, and reads are capped."""
        path = tmp_path / "chrome"
        self._elf_with_needed(path, 3)
        monkeypatch.setattr(mod, "_MAX_SONAMES", 2)
        reads: list[int] = []
        real_open = mod.os.fdopen

        def counting_fdopen(fd, mode="r", *args, **kwargs):
            fh = real_open(fd, mode, *args, **kwargs)
            real_read = fh.read

            def read(n=-1):
                reads.append(n)
                return real_read(n)

            fh.read = read
            return fh

        monkeypatch.setattr(mod.os, "fdopen", counting_fdopen)

        names = mod._needed_sonames(path)

        assert len(names) == 2
        assert reads.count(mod._MAX_SONAME_LEN) == 2
