"""A WebKit download on Amazon Linux ends as "unsupported here", not as a generic error.

MEASURED on Amazon Linux 2023 (``ID=amzn``, ``ID_LIKE=fedora``): ``install-browser
webkit`` downloads the build, prints Playwright's host-validation warning naming
27 missing libraries, and exits 0. Most of those libraries have no package in the
distribution's repositories and Playwright's own dependency installer is apt-only,
so a detail that ends in "install the libraries ... then retry" asks for something
the operator cannot do. The outcome has to say that WebKit is not supported on
this operating system and which engine is.

Two bounds keep that verdict honest. It is keyed on the distribution measured
(the os-release ``ID``), not on the rpm family: Fedora packages several of those
libraries, so its operator keeps the instruction. And it rides only on a step whose
output names the missing libraries: a download that failed for another reason is
reported as that failure, not as a platform limit.

Everything else keeps its remedy: Firefox on the same host passes host validation
(measured in the same session), so a Firefox library gap stays the manual
instruction; Chromium gets its package command; every engine on an apt host gets
``install-deps``.
"""

from __future__ import annotations

import platform
import shutil

import pytest

from kiro_crew import platform_compat
from kiro_crew.browser_cli import install as install_mod
from kiro_crew.browser_cli import install_job, os_deps

#: stderr of ``playwright-cli install-browser webkit`` on Amazon Linux 2023, exit 0.
#: Verbatim apart from the Node prefix in the stack trace, which is a placeholder.
REAL_STDERR = (
    "\n"
    "╔════════════════════════════════════════════════════════════════════╗\n"
    "║ Update available for @playwright/cli: 0.1.18 → 0.1.22              ║\n"
    "║ Run `npm install -g @playwright/cli@latest` (global) or            ║\n"
    "║ `npm install --save-dev @playwright/cli@latest` (local) to update. ║\n"
    "╚════════════════════════════════════════════════════════════════════╝\n"
    "\n"
    "Playwright Host validation warning: \n"
    "╔══════════════════════════════════════════════════════╗\n"
    "║ Host system is missing dependencies to run browsers. ║\n"
    "║ Missing libraries:                                   ║\n"
    "║     libgtk-4.so.1                                    ║\n"
    "║     libvulkan.so.1                                   ║\n"
    "║     libicudata.so.74                                 ║\n"
    "║     libicui18n.so.74                                 ║\n"
    "║     libatomic.so.1                                   ║\n"
    "║     libicuuc.so.74                                   ║\n"
    "║     libgstcodecparsers-1.0.so.0                      ║\n"
    "║     libflite.so.1                                    ║\n"
    "║     libflite_usenglish.so.1                          ║\n"
    "║     libflite_cmu_grapheme_lang.so.1                  ║\n"
    "║     libflite_cmu_grapheme_lex.so.1                   ║\n"
    "║     libflite_cmu_indic_lang.so.1                     ║\n"
    "║     libflite_cmu_indic_lex.so.1                      ║\n"
    "║     libflite_cmulex.so.1                             ║\n"
    "║     libflite_cmu_time_awb.so.1                       ║\n"
    "║     libflite_cmu_us_awb.so.1                         ║\n"
    "║     libflite_cmu_us_kal16.so.1                       ║\n"
    "║     libflite_cmu_us_kal.so.1                         ║\n"
    "║     libflite_cmu_us_rms.so.1                         ║\n"
    "║     libflite_cmu_us_slt.so.1                         ║\n"
    "║     libavif.so.16                                    ║\n"
    "║     libharfbuzz-icu.so.0                             ║\n"
    "║     libjpeg.so.8                                     ║\n"
    "║     libmanette-0.2.so.0                              ║\n"
    "║     libhyphen.so.0                                   ║\n"
    "║     libGLESv2.so.2                                   ║\n"
    "║     libx264.so                                       ║\n"
    "╚══════════════════════════════════════════════════════╝\n"
    "    at validateDependenciesLinux (<node-prefix>/lib/node_modules/@playwright/cli"
    "/node_modules/playwright-core/lib/coreBundle.js:32000:9)\n"
    "    at async Registry._validateHostRequirements (<node-prefix>/lib/node_modules"
    "/@playwright/cli/node_modules/playwright-core/lib/coreBundle.js:33262:18)\n"
    "    at async Registry._validateHostRequirementsForExecutableIfNeeded (<node-prefix>"
    "/lib/node_modules/@playwright/cli/node_modules/playwright-core/lib/coreBundle.js:33384:11)\n"
    "    at async Registry.validateHostRequirementsForExecutablesIfNeeded (<node-prefix>"
    "/lib/node_modules/@playwright/cli/node_modules/playwright-core/lib/coreBundle.js:33373:11)\n"
    "    at async installBrowsers (<node-prefix>/lib/node_modules/@playwright/cli"
    "/node_modules/playwright-core/lib/coreBundle.js:69649:5)\n"
    "    at async _Command.<anonymous> (<node-prefix>/lib/node_modules/@playwright/cli"
    "/node_modules/playwright-core/lib/coreBundle.js:73188:7)"
)

#: stdout of the same run, progress bars dropped. The download itself succeeds.
REAL_STDOUT = (
    "BEWARE: your OS is not officially supported by Playwright; downloading fallback "
    "build for ubuntu24.04-x64.\n"
    "Downloading WebKit 26.5 (playwright webkit v2342) from https://cdn.playwright.dev"
    "/dbazure/download/playwright/builds/webkit/2342/webkit-ubuntu-24.04.zip\n"
    "WebKit 26.5 (playwright webkit v2342) downloaded to <browsers-path>/webkit-2342\n"
    "BEWARE: your OS is not officially supported by Playwright; downloading fallback "
    "build for ubuntu24.04-x64.\n"
    "Downloading FFmpeg (playwright ffmpeg v1011) from https://cdn.playwright.dev"
    "/dbazure/download/playwright/builds/ffmpeg/1011/ffmpeg-linux.zip\n"
    "FFmpeg (playwright ffmpeg v1011) downloaded to <browsers-path>/ffmpeg-1011\n"
)

#: stderr of ``playwright-cli install-browser firefox`` on the same host, exit 0:
#: the update banner alone. Firefox's build passes host validation there.
REAL_FIREFOX_STDERR = (
    "\n"
    "╔════════════════════════════════════════════════════════════════════╗\n"
    "║ Update available for @playwright/cli: 0.1.18 → 0.1.22              ║\n"
    "║ Run `npm install -g @playwright/cli@latest` (global) or            ║\n"
    "║ `npm install --save-dev @playwright/cli@latest` (local) to update. ║\n"
    "╚════════════════════════════════════════════════════════════════════╝\n"
)

UNSUPPORTED = "is not supported on this operating system"
SUPPORTED_ENGINE = "Chromium is the supported browser engine on this host."
#: The retry instruction the rpm family gets for an engine without a package list.
RETRY_INSTRUCTION = "then retry the download"


@pytest.fixture(autouse=True)
def _clear_family_cache():
    os_deps.linux_family.cache_clear()
    yield
    os_deps.linux_family.cache_clear()


def _host(monkeypatch: pytest.MonkeyPatch, fields: dict[str, str], managers: set[str]) -> None:
    """Fake the os-release reader and the ``PATH`` probe; the test host is not the subject."""
    monkeypatch.setattr(platform, "freedesktop_os_release", lambda: dict(fields))
    monkeypatch.setattr(platform_compat, "IS_LINUX", True)
    monkeypatch.setattr(
        shutil, "which", lambda name: f"/usr/bin/{name}" if name in managers else None
    )


def _amazon_linux_2023(monkeypatch: pytest.MonkeyPatch) -> None:
    _host(monkeypatch, {"ID": "amzn", "ID_LIKE": "fedora", "VERSION_ID": "2023"}, {"dnf", "sudo"})


def _fedora(monkeypatch: pytest.MonkeyPatch) -> None:
    """An rpm host no capture was taken on; Fedora packages several of the
    libraries Amazon Linux lacks, so its operator can follow the instruction."""
    _host(monkeypatch, {"ID": "fedora", "VERSION_ID": "42"}, {"dnf", "sudo"})


def _ubuntu(monkeypatch: pytest.MonkeyPatch) -> None:
    _host(monkeypatch, {"ID": "ubuntu", "ID_LIKE": "debian"}, {"sudo"})


def _cli_prints(
    monkeypatch: pytest.MonkeyPatch, rc: int, stdout: str, stderr: str
) -> list[list[str]]:
    """Resolve a fake CLI and make every ``install-browser`` spawn answer the same way."""
    calls: list[list[str]] = []
    monkeypatch.setattr(install_mod, "cli_path", lambda: "/n/playwright-cli")
    monkeypatch.setattr(install_mod, "cli_command", lambda cli=None: [cli or "/n/playwright-cli"])

    def fake_run(argv: list[str], timeout: float) -> tuple[int, str, str]:
        calls.append(list(argv))
        return rc, stdout, stderr

    monkeypatch.setattr(install_mod, "_run", fake_run)
    return calls


def _outcome(engine: str) -> tuple[str, str | None, str]:
    """The installer's result as the dashboard job records it: status, code, detail."""
    status, code, detail = install_job.outcome_of(
        install_mod.install_browser(engine), "install-browser"
    )
    assert detail is not None
    return status, code, detail


class TestWebKitOnAnRpmHost:
    def test_the_host_validation_warning_reports_webkit_as_unsupported_here(self, monkeypatch):
        _amazon_linux_2023(monkeypatch)
        _cli_prints(monkeypatch, 0, REAL_STDOUT, REAL_STDERR)

        status, code, detail = _outcome("webkit")

        # Still a failed step with the same code: a build that cannot launch is not
        # a success, and the panel's failure rendering is what shows the detail.
        assert (status, code) == ("failed", "step_failed")
        assert f"WebKit {UNSUPPORTED}" in detail
        assert SUPPORTED_ENGINE in detail
        # The instruction nobody on this host can follow is gone...
        assert RETRY_INSTRUCTION not in detail
        assert "no verified package list" not in detail
        # ...Chromium's package set is not offered as the fix...
        assert "dnf install" not in detail
        # ...and Playwright's own evidence stays readable above the verdict, which
        # is last so that truncation of a long library list never eats it.
        assert "libx264.so" in detail
        assert detail.endswith(SUPPORTED_ENGINE)

    def test_the_verdict_is_the_hint_of_the_step_the_installer_returns(self, monkeypatch):
        """The job layer reads the last step's ``hint``; the verdict must be there,
        not only in the stderr text, or a detail rebuilt from the step loses it."""
        _amazon_linux_2023(monkeypatch)
        _cli_prints(monkeypatch, 0, REAL_STDOUT, REAL_STDERR)

        result = install_mod.install_browser("webkit")

        assert result["ok"] is False
        step = result["steps"][-1]
        assert step["name"] == "install-browser-webkit"
        # rc stays 0: the exit code is honestly reported, it is just not the verdict.
        assert step["returncode"] == 0
        assert f"WebKit {UNSUPPORTED}" in step["hint"]
        assert step["stderr"].endswith(step["hint"])

    def test_a_webkit_download_that_fails_for_another_reason_keeps_its_own_error(self, monkeypatch):
        """The verdict is a claim about the host, and its only evidence is
        Playwright's report of the missing libraries. A download that failed before
        that report (a 503, a full disk) is not a platform limit, so it gets no
        hint at all: neither the verdict nor an instruction to install libraries
        nobody named."""
        _amazon_linux_2023(monkeypatch)
        _cli_prints(
            monkeypatch,
            1,
            "",
            "Error: Download failed: server returned code 503. URL: https://cdn.playwright.dev/x",
        )

        result = install_mod.install_browser("webkit")
        _status, _code, detail = _outcome("webkit")

        assert result["ok"] is False
        assert result["steps"][-1]["hint"] == ""
        assert "server returned code 503" in detail
        assert UNSUPPORTED not in detail
        assert RETRY_INSTRUCTION not in detail
        assert "no verified package list" not in detail

    def test_the_missing_library_report_decides_not_the_exit_code(self, monkeypatch):
        """A CLI that exits non-zero with the same host-validation box still names
        the libraries, so the verdict rides on it as on the exit-0 shape."""
        _amazon_linux_2023(monkeypatch)
        _cli_prints(monkeypatch, 1, "", REAL_STDERR)

        _status, _code, detail = _outcome("webkit")

        assert f"WebKit {UNSUPPORTED}" in detail
        assert detail.endswith(SUPPORTED_ENGINE)

    def test_a_clean_download_of_webkit_still_succeeds(self, monkeypatch):
        """The verdict is failure-only: a WebKit build that passes host validation
        must not be reported as unsupported."""
        _amazon_linux_2023(monkeypatch)
        _cli_prints(monkeypatch, 0, REAL_STDOUT, "")

        result = install_mod.install_browser("webkit")

        assert result["ok"] is True
        assert install_job.outcome_of(result, "install-browser") == ("succeeded", None, None)
        assert all(step["stderr"] == "" for step in result["steps"])


class TestEverythingElseIsUnchanged:
    def test_webkit_on_fedora_keeps_the_manual_instruction(self, monkeypatch):
        """One host's measurement is that host's verdict. Fedora was not measured
        and packages gtk4, ICU, libavif, flite, libmanette and hyphen, so the same
        missing-library report there stays an instruction the operator can follow."""
        _fedora(monkeypatch)
        _cli_prints(monkeypatch, 0, REAL_STDOUT, REAL_STDERR)

        status, code, detail = _outcome("webkit")

        assert (status, code) == ("failed", "step_failed")
        assert "no verified package list for WebKit" in detail
        assert RETRY_INSTRUCTION in detail
        assert UNSUPPORTED not in detail
        assert "libx264.so" in detail

    def test_firefox_on_the_same_host_downloads_and_is_not_called_unsupported(self, monkeypatch):
        """MEASURED: Firefox passes host validation on Amazon Linux 2023."""
        _amazon_linux_2023(monkeypatch)
        _cli_prints(monkeypatch, 0, REAL_STDOUT, REAL_FIREFOX_STDERR)

        result = install_mod.install_browser("firefox")

        assert result["ok"] is True
        assert install_job.outcome_of(result, "install-browser") == ("succeeded", None, None)

    def test_a_firefox_library_gap_keeps_the_manual_instruction(self, monkeypatch):
        """Firefox's libraries can be installed by name on this family, so a
        host-validation warning for Firefox stays an instruction, not a verdict."""
        _amazon_linux_2023(monkeypatch)
        _cli_prints(monkeypatch, 0, REAL_STDOUT, REAL_STDERR)

        status, code, detail = _outcome("firefox")

        assert (status, code) == ("failed", "step_failed")
        assert "no verified package list for Firefox" in detail
        assert RETRY_INSTRUCTION in detail
        assert UNSUPPORTED not in detail

    def test_chromium_on_the_same_host_keeps_its_package_command(self, monkeypatch):
        _amazon_linux_2023(monkeypatch)
        _cli_prints(monkeypatch, 0, REAL_STDOUT, REAL_STDERR)

        status, code, detail = _outcome("chromium")

        assert (status, code) == ("failed", "step_failed")
        assert "sudo dnf install -y " in detail
        assert "mesa-libgbm" in detail
        assert UNSUPPORTED not in detail

    @pytest.mark.parametrize("engine", ["webkit", "firefox", "chromium"])
    def test_an_apt_host_keeps_install_deps_for_every_engine(self, monkeypatch, engine):
        _ubuntu(monkeypatch)
        calls = _cli_prints(monkeypatch, 0, REAL_STDOUT, REAL_STDERR)

        status, code, detail = _outcome(engine)

        # The flag is tried first and the remedy rides on the retry without it.
        assert [argv[-1] for argv in calls] == ["--with-deps", engine]
        assert (status, code) == ("failed", "step_failed")
        assert f"sudo npx playwright install-deps {engine}" in detail
        assert UNSUPPORTED not in detail
