"""Detection, install sequencing, and the presence-is-consent gate."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from kiro_crew.browser_cli import install as mod

# The real implementation, captured before the autouse fixture below replaces
# ``mod._required_revisions`` with a degradation stub. Revision-aware tests
# restore THIS (re-reading ``mod._required_revisions`` there would just re-bind
# the stub to itself, silently leaving every test on the fallback path).
_REAL_REQUIRED_REVISIONS = mod._required_revisions


@pytest.fixture(autouse=True)
def isolated_browser_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep ``browser_ok`` off the developer's real Playwright cache.

    Also defaults ``_required_revisions`` to ``None`` so a test that does not
    opt into revision-awareness exercises the documented degradation path
    (presence-only prefix match) deterministically -- independent of whether the
    host running the suite happens to have a ``playwright-cli`` on PATH whose
    ``browsers.json`` would otherwise be read. Tests that assert revision-exact
    behaviour override this explicitly.
    """
    cache = tmp_path / "ms-playwright"
    cache.mkdir()
    monkeypatch.setattr(mod, "_browsers_cache_dir", lambda: cache)
    monkeypatch.setattr(mod, "_required_revisions", lambda: None)
    return cache


@pytest.fixture(autouse=True)
def _default_no_os_deps(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default every test to a host with no OS-package step.

    The browser step now asks :mod:`kiro_crew.browser_cli.os_deps` what this host
    allows, and the answer is read from the DEVELOPER's ``/etc/os-release``
    otherwise -- which would make the argv assertions here pass on macOS and fail
    on Ubuntu. Tests that care about the flag opt in explicitly.
    """
    monkeypatch.setattr(mod.os_deps, "with_deps_supported", lambda: False)
    monkeypatch.setattr(mod.os_deps, "missing_deps_hint", lambda: "")


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    tools: dict[str, str],
    results: dict[str, tuple[int, str, str]] | None = None,
) -> list[list[str]]:
    """Fake tool resolution and subprocess layer; return the recorded argv list.

    *results* is keyed on the argv's first token so a test states only the
    outcomes it cares about; anything unlisted succeeds silently.
    """
    calls: list[list[str]] = []
    outcomes = results or {}

    monkeypatch.setattr(mod, "find_node_tool", lambda name, base_path=None: tools.get(name))

    def fake_run(argv: list[str], timeout: float) -> tuple[int, str, str]:
        calls.append(list(argv))
        return outcomes.get(argv[0], (0, "", ""))

    monkeypatch.setattr(mod, "_run", fake_run)
    return calls


def test_detect_reports_absent_when_binary_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    _wire(monkeypatch, {"node": "/n/node"}, {"/n/node": (0, "v22.1.0", "")})

    d = mod.detect()

    assert d["installed"] is False
    assert d["cli_path"] is None
    assert d["cli_version"] is None
    # Node being fine must not be reported as the CLI being present.
    assert d["node_ok"] is True


def test_detect_reports_version_when_present(monkeypatch: pytest.MonkeyPatch) -> None:
    _wire(
        monkeypatch,
        {"node": "/n/node", "playwright-cli": "/n/playwright-cli"},
        {"/n/node": (0, "v22.1.0", ""), "/n/playwright-cli": (0, "0.1.18\n", "")},
    )

    d = mod.detect()

    assert d["installed"] is True
    assert d["cli_path"] == "/n/playwright-cli"
    assert d["cli_version"] == "0.1.18"


@pytest.mark.parametrize(
    ("reported", "expect_ok"),
    [
        ("v18.20.5", False),
        ("v19.9.0", False),
        ("v20.0.0", True),
        ("v24.18.0", True),
    ],
)
def test_detect_enforces_node_20_floor(
    monkeypatch: pytest.MonkeyPatch, reported: str, expect_ok: bool
) -> None:
    """Node below 20 is rejected, and exactly 20 is accepted."""
    _wire(monkeypatch, {"node": "/n/node"}, {"/n/node": (0, reported, "")})

    d = mod.detect()

    assert d["node_ok"] is expect_ok
    assert d["node_version"] == reported.lstrip("v")


def test_detect_node_absent_is_not_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    _wire(monkeypatch, {})

    d = mod.detect()

    assert d["node_ok"] is False
    assert d["node_version"] is None


def test_detect_browser_ok_requires_chromium_build(
    monkeypatch: pytest.MonkeyPatch, isolated_browser_cache: Path
) -> None:
    _wire(monkeypatch, {})
    assert mod.detect()["browser_ok"] is False

    # A non-chromium engine is not enough: attach/extension mode is chromium-only.
    (isolated_browser_cache / "firefox-1489").mkdir()
    assert mod.detect()["browser_ok"] is False

    (isolated_browser_cache / "chromium-1200").mkdir()
    assert mod.detect()["browser_ok"] is True


def test_available_is_false_without_the_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    _wire(monkeypatch, {"node": "/n/node"}, {"/n/node": (0, "v22.1.0", "")})

    assert mod.available() is False


def test_available_is_presence_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Presence is consent: a broken Node or missing browser does not revoke it.

    Reporting "not consented" for a repairable environment would send the
    operator to the wrong fix, and there is no toggle that could say otherwise.
    """
    _wire(
        monkeypatch,
        {"playwright-cli": "/n/playwright-cli"},
        {"/n/playwright-cli": (0, "0.1.18", "")},
    )

    assert mod.available() is True
    assert mod.detect()["node_ok"] is False
    assert mod.detect()["browser_ok"] is False


def test_no_consent_flag_is_consulted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The gate reads PATH and nothing else -- no flag file, no config key.

    An empty data home must not make an installed CLI unavailable, which is what
    a re-introduced consent file would do.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "empty-home"))
    _wire(
        monkeypatch,
        {"playwright-cli": "/n/playwright-cli"},
        {"/n/playwright-cli": (0, "0.1.18", "")},
    )

    assert mod.available() is True


def test_install_aborts_when_npm_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _wire(monkeypatch, {})

    result = mod.install()

    assert result["ok"] is False
    assert [s["name"] for s in result["steps"]] == ["npm-install-global"]
    assert "npm not found" in result["steps"][0]["stderr"]
    assert calls == []


def test_install_runs_all_three_steps_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _wire(monkeypatch, {"npm": "/n/npm", "playwright-cli": "/n/playwright-cli"})

    result = mod.install()

    assert result["ok"] is True
    assert [s["name"] for s in result["steps"]] == [
        "npm-install-global",
        "install-browser",
        "install-skills",
    ]
    assert calls[0] == ["/n/npm", "install", "-g", "@playwright/cli@latest"]
    assert calls[1] == ["/n/playwright-cli", "install-browser"]
    assert calls[2] == [
        "/n/playwright-cli",
        "install",
        "--skills",
        "agents",
        "--global",
    ]


def test_install_adds_with_deps_only_where_the_host_honours_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--with-deps`` drives the system package manager, and Playwright's
    implementation of it is apt-only, so the flag is gated on the host family
    rather than on "is Linux"."""
    monkeypatch.setattr(mod.os_deps, "with_deps_supported", lambda: True)
    apt_calls = _wire(monkeypatch, {"npm": "/n/npm", "playwright-cli": "/n/pw"})
    mod.install()
    assert ["/n/pw", "install-browser", "--with-deps"] in apt_calls

    monkeypatch.setattr(mod.os_deps, "with_deps_supported", lambda: False)
    other_calls = _wire(monkeypatch, {"npm": "/n/npm", "playwright-cli": "/n/pw"})
    mod.install()
    assert ["/n/pw", "install-browser"] in other_calls
    assert all("--with-deps" not in argv for argv in other_calls)


def test_install_falls_back_without_deps_when_the_package_step_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refused ``apt-get`` must not cost the operator the browser.

    Regression for a real dev-desktop failure: ``--with-deps`` shells out to
    ``apt-get`` as root, sudo policy refuses it, and because the flag and the
    download are one CLI invocation the download failed too -- even though it
    needs no privilege at all.
    """
    monkeypatch.setattr(mod.os_deps, "with_deps_supported", lambda: True)
    monkeypatch.setattr(mod.os_deps, "missing_deps_hint", lambda: "run this: sudo apt-get ...")
    calls = _wire(
        monkeypatch,
        {"npm": "/n/npm", "playwright-cli": "/n/pw"},
        # Keyed on argv[0], so this fails BOTH browser attempts and the skills
        # step too; the with-deps branch is distinguished below by argv content.
    )

    def fake_run(argv: list[str], timeout: float) -> tuple[int, str, str]:
        calls.append(list(argv))
        if "--with-deps" in argv:
            return (
                1,
                "",
                "Sorry, user bolichen is not allowed to execute "
                "'/bin/sh -c apt-get update' as root on dev-dsk-example.",
            )
        return (0, "", "")

    monkeypatch.setattr(mod, "_run", fake_run)

    result = mod.install()

    assert result["ok"] is True
    assert [s["name"] for s in result["steps"]] == [
        "npm-install-global",
        "install-browser",
        "install-browser-no-deps",
        "install-skills",
    ]
    # The refused attempt stays visible rather than being swallowed...
    assert result["steps"][1]["ok"] is False
    # ...but it must not veto an install the retry completed.
    assert result["steps"][2]["ok"] is True
    assert ["/n/pw", "install-browser", "--with-deps"] in calls
    assert ["/n/pw", "install-browser"] in calls


def test_a_zero_exit_carrying_the_host_validation_warning_is_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MEASURED: ``install-browser`` exits 0 when the host is missing libraries.

    Playwright classifies it as a warning, so trusting the exit code reports a
    browser that cannot launch as installed -- the panel goes green and the real
    error arrives at the user's first browse as an opaque stack trace. The step
    must fail, and must carry the remedy.
    """
    monkeypatch.setattr(mod.os_deps, "missing_deps_hint", lambda: "sudo dnf install -y nss")
    warning = (
        "Playwright Host validation warning: \n"
        "Host system is missing dependencies to run browsers.\n"
        "Missing libraries:\n    libgtk-4.so.1\n"
    )
    _wire(
        monkeypatch,
        {"npm": "/n/npm", "playwright-cli": "/n/pw"},
        {"/n/pw": (0, "", warning)},
    )

    result = mod.install()

    assert result["ok"] is False
    browser_step = result["steps"][1]
    assert browser_step["name"] == "install-browser"
    assert browser_step["ok"] is False
    # rc stays 0 -- the exit code is honestly reported, it is just not the verdict.
    assert browser_step["returncode"] == 0
    assert "missing dependencies" in browser_step["stderr"]
    assert "sudo dnf install -y nss" in browser_step["stderr"]
    # The skills step never runs behind a browser that cannot launch.
    assert [s["name"] for s in result["steps"]] == ["npm-install-global", "install-browser"]


def test_the_host_validation_warning_is_caught_on_stdout_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The diagnostic is on stderr today; a version that moves it to stdout must
    not silently reopen the bug."""
    _wire(
        monkeypatch,
        {"npm": "/n/npm", "playwright-cli": "/n/pw"},
        {"/n/pw": (0, "Host system is missing dependencies to run browsers.", "")},
    )

    result = mod.install()

    assert result["ok"] is False
    assert result["steps"][1]["ok"] is False
    assert "missing dependencies" in result["steps"][1]["stderr"]


def test_an_ordinary_zero_exit_browser_step_still_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The signal must not fail an install that actually worked: Playwright writes
    progress and download notices to stderr on a healthy run."""
    _wire(
        monkeypatch,
        {"npm": "/n/npm", "playwright-cli": "/n/pw"},
        {"/n/pw": (0, "", "Downloading Chromium 141.0 (playwright build v1237)")},
    )

    result = mod.install()

    assert result["ok"] is True
    assert [s["name"] for s in result["steps"]] == [
        "npm-install-global",
        "install-browser",
        "install-skills",
    ]


def test_install_still_fails_when_the_no_deps_retry_also_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fallback is a retry, not a guarantee: a real download failure stays fatal.

    Also pins WHERE the remedy lands. It belongs on the retry, which is the
    attempt that actually ran without the package step; putting it on the
    with-deps attempt would hang a remediation command on a failure the retry may
    well have recovered from.
    """
    monkeypatch.setattr(mod.os_deps, "with_deps_supported", lambda: True)
    monkeypatch.setattr(mod.os_deps, "missing_deps_hint", lambda: "sudo dnf install -y nss")
    calls = _wire(
        monkeypatch,
        {"npm": "/n/npm", "playwright-cli": "/n/pw"},
        {"/n/pw": (1, "", "network unreachable")},
    )

    result = mod.install()

    assert result["ok"] is False
    assert [s["name"] for s in result["steps"]] == [
        "npm-install-global",
        "install-browser",
        "install-browser-no-deps",
    ]
    assert "sudo dnf install -y nss" not in result["steps"][1]["stderr"]
    assert "sudo dnf install -y nss" in result["steps"][2]["stderr"]
    assert all("--skills" not in argv for argv in calls)


def test_a_failed_browser_step_carries_the_manual_remedy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On a host whose libraries only root can install, the failure detail must
    carry the command that resolves it -- the settings panel shows that detail
    verbatim, so this is the whole remediation surface."""
    monkeypatch.setattr(mod.os_deps, "missing_deps_hint", lambda: "sudo dnf install -y nss")
    _wire(
        monkeypatch,
        {"npm": "/n/npm", "playwright-cli": "/n/pw"},
        {"/n/pw": (1, "", "Host system is missing dependencies!")},
    )

    result = mod.install()

    assert result["ok"] is False
    detail = result["steps"][-1]["stderr"]
    assert "Host system is missing dependencies!" in detail
    assert "sudo dnf install -y nss" in detail


def test_the_remedy_survives_a_stderr_long_enough_to_hit_the_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hint is appended AFTER truncation. Appending before it would let a
    verbose package manager push the one actionable line out of view."""
    monkeypatch.setattr(mod.os_deps, "missing_deps_hint", lambda: "sudo dnf install -y nss")
    _wire(
        monkeypatch,
        {"npm": "/n/npm", "playwright-cli": "/n/pw"},
        {"/n/pw": (1, "", "x" * 50_000)},
    )

    result = mod.install()

    assert "sudo dnf install -y nss" in result["steps"][-1]["stderr"]


def test_a_successful_step_carries_no_remedy(monkeypatch: pytest.MonkeyPatch) -> None:
    """The hint is failure-only: on a green install it would read as a warning."""
    monkeypatch.setattr(mod.os_deps, "missing_deps_hint", lambda: "sudo dnf install -y nss")
    _wire(monkeypatch, {"npm": "/n/npm", "playwright-cli": "/n/pw"})

    result = mod.install()

    assert result["ok"] is True
    assert all(s["stderr"] == "" for s in result["steps"])


def test_install_stops_at_the_first_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Later steps depend on the binary the first one installs."""
    calls = _wire(
        monkeypatch,
        {"npm": "/n/npm", "playwright-cli": "/n/pw"},
        {"/n/npm": (1, "", "E401 Unauthorized")},
    )

    result = mod.install()

    assert result["ok"] is False
    assert [s["name"] for s in result["steps"]] == ["npm-install-global"]
    assert result["steps"][0]["stderr"] == "E401 Unauthorized"
    assert result["steps"][0]["returncode"] == 1
    assert len(calls) == 1


def test_install_reports_binary_unresolvable_after_npm_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A green npm step with no resolvable binary is a failure, not a success."""
    _wire(monkeypatch, {"npm": "/n/npm"})

    result = mod.install()

    assert result["ok"] is False
    assert [s["name"] for s in result["steps"]] == [
        "npm-install-global",
        "resolve-binary",
    ]


def test_install_browser_failure_skips_skills(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _wire(
        monkeypatch,
        {"npm": "/n/npm", "playwright-cli": "/n/pw"},
        {"/n/pw": (1, "", "download failed")},
    )

    result = mod.install()

    assert result["ok"] is False
    assert [s["name"] for s in result["steps"]] == ["npm-install-global", "install-browser"]
    assert all("--skills" not in argv for argv in calls)


def test_step_success_does_not_surface_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    """npm writes progress and deprecation notices to stderr on a good install."""
    _wire(
        monkeypatch,
        {"npm": "/n/npm", "playwright-cli": "/n/pw"},
        {"/n/npm": (0, "", "npm warn deprecated foo@1.0.0")},
    )

    result = mod.install()

    assert result["steps"][0]["ok"] is True
    assert result["steps"][0]["stderr"] == ""


def test_cli_env_layers_node_dirs_over_the_broad_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Node bins win, and the broad non-login PATH is layered under them.

    A global npm bin dir the gateway never had on PATH must still be found
    (node layer, outermost). The broad layer under it carries ``~/.local/bin`` /
    Homebrew's bin so a mise-managed npm's post-install ``mise reshim`` hook can
    find the ``mise`` binary instead of dying ``mise: command not found``.
    """
    monkeypatch.setattr(mod, "augmented_path", lambda base: f"/home/.local/bin:{base}")
    monkeypatch.setattr(mod, "node_augmented_path", lambda base: f"/node/bin:{base}")
    monkeypatch.setenv("PATH", "/usr/bin")

    assert mod.cli_env()["PATH"] == "/node/bin:/home/.local/bin:/usr/bin"


@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX ~ expansion and the ~/.local/bin (mise) layout; Windows uses a different PATH set",
)
def test_cli_env_integration_puts_mise_home_bin_on_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The REAL (unstubbed) broad layer must contribute ``~/.local/bin``.

    The stubbed unit tests above lock only the composition order. This one runs
    the real ``augmented_path`` so it fails if a refactor drops ``~/.local/bin``
    from the broad PATH -- the dir where the ``mise`` binary lives, without which
    the post-install ``mise reshim`` hook dies ``mise: command not found`` and
    ``npm install -g`` fails rc 127. That regression would otherwise be invisible
    to this suite.
    """
    home = tmp_path / "home"
    local_bin = home / ".local" / "bin"
    local_bin.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    # Isolate mise's data dir so the assertion does not depend on the host's.
    monkeypatch.delenv("MISE_DATA_DIR", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setenv("PATH", "/usr/bin")

    path_entries = mod.cli_env()["PATH"].split(os.pathsep)

    assert str(local_bin) in path_entries


class TestPerEngineDownloads:
    """Each engine is its own download, and the engine name never reaches argv raw."""

    def test_engines_are_reported_individually(self, monkeypatch, tmp_path):
        cache = tmp_path / "ms-playwright"
        cache.mkdir(exist_ok=True)
        (cache / "chromium-1208").mkdir()
        (cache / "webkit-2248").mkdir()
        monkeypatch.setattr(mod, "_browsers_cache_dir", lambda: cache)

        assert mod.browsers_present() == {
            "chromium": True,
            "firefox": False,
            "webkit": True,
        }
        # The capability gate stays Chromium-only: attach needs that engine, so a
        # cache holding only WebKit must not read as "browsing works".
        assert mod._browser_present() is True

    def test_an_unreadable_cache_reports_absent_rather_than_raising(self, monkeypatch):
        monkeypatch.setattr(mod, "_browsers_cache_dir", lambda: None)
        assert mod.browsers_present() == {
            "chromium": False,
            "firefox": False,
            "webkit": False,
        }

    def test_an_unknown_engine_is_refused_before_it_reaches_argv(self, monkeypatch):
        called: list[list[str]] = []
        monkeypatch.setattr(mod, "_step", lambda *a, **k: called.append(a[1]) or {"ok": True})

        result = mod.install_browser("firefox; rm -rf /")

        assert result["ok"] is False
        assert called == [], "a rejected engine must never be spawned"
        assert "unknown engine" in result["steps"][0]["stderr"]

    def test_a_known_engine_is_passed_through(self, monkeypatch, tmp_path):
        fake_cli = tmp_path / "playwright-cli"
        fake_cli.write_text("")
        monkeypatch.setattr(mod, "cli_path", lambda: str(fake_cli))
        seen: list[list[str]] = []

        def _fake_step(name, argv, timeout, hint="", failure_signal=None):
            seen.append(argv)
            return {"name": name, "ok": True, "returncode": 0}

        monkeypatch.setattr(mod, "_step", _fake_step)

        result = mod.install_browser("firefox")

        assert result["ok"] is True
        assert [str(t) for t in seen[0][:3]] == [str(fake_cli), "install-browser", "firefox"]

    def test_it_refuses_when_the_cli_is_absent(self, monkeypatch):
        monkeypatch.setattr(mod, "cli_path", lambda: None)
        result = mod.install_browser("chromium")
        assert result["ok"] is False
        assert result["steps"][0]["name"] == "resolve-binary"

    def test_a_refused_package_step_falls_back_to_a_plain_retry(self, monkeypatch, tmp_path):
        """The per-engine path shares `_download_browser` with `install()`, so a
        host that refuses the package manager must not cost it the download here
        either."""
        monkeypatch.setattr(mod.os_deps, "with_deps_supported", lambda: True)
        fake_cli = tmp_path / "playwright-cli"
        fake_cli.write_text("")
        monkeypatch.setattr(mod, "cli_path", lambda: str(fake_cli))

        seen: list[list[str]] = []

        def fake_run(argv, timeout):
            seen.append(list(argv))
            if "--with-deps" in argv:
                return (1, "", "not allowed to execute ... as root")
            return (0, "", "")

        monkeypatch.setattr(mod, "_run", fake_run)

        result = mod.install_browser("firefox")

        assert result["ok"] is True
        assert [s["name"] for s in result["steps"]] == [
            "install-browser-firefox",
            "install-browser-firefox-no-deps",
        ]
        assert [str(fake_cli), "install-browser", "firefox", "--with-deps"] in seen
        assert [str(fake_cli), "install-browser", "firefox"] in seen

    def test_the_engine_still_reaches_argv_once_on_a_host_without_the_flag(
        self, monkeypatch, tmp_path
    ):
        """The no-flag path must keep the engine argument: dropping it would
        silently download Chromium while reporting the engine the user asked for."""
        fake_cli = tmp_path / "playwright-cli"
        fake_cli.write_text("")
        monkeypatch.setattr(mod, "cli_path", lambda: str(fake_cli))
        seen: list[list[str]] = []
        monkeypatch.setattr(mod, "_run", lambda argv, t: (seen.append(list(argv)), (0, "", ""))[1])

        result = mod.install_browser("webkit")

        assert result["ok"] is True
        assert seen == [[str(fake_cli), "install-browser", "webkit"]]


class TestFailureDetailIsRedactedAtTheSource:
    """npm quotes the environment back on failure, and the log outlives the UI."""

    def test_a_credential_in_stderr_is_redacted_before_logging_or_returning(
        self, monkeypatch, caplog
    ):
        leak = (
            "npm error code E401\n"
            "npm error Incorrect or missing password.\n"
            "npm error registry https://npm.internal.example.com/"
            "?_authToken=abcd1234secrettokenvalue\n"
        )
        monkeypatch.setattr(mod, "_run", lambda argv, timeout: (1, "", leak))

        with caplog.at_level("WARNING"):
            step = mod._step("npm-install-global", ["npm", "install"], 1.0)

        assert step["ok"] is False
        # Neither the returned detail nor the log line may carry the token.
        assert "abcd1234secrettokenvalue" not in step["stderr"]
        assert "abcd1234secrettokenvalue" not in caplog.text
        # ...and the useful part survives, or the redaction would be useless.
        assert "E401" in step["stderr"]

    def test_a_huge_stderr_is_capped(self, monkeypatch):
        monkeypatch.setattr(mod, "_run", lambda argv, timeout: (1, "", "x" * 50_000))
        step = mod._step("npm-install-global", ["npm", "install"], 1.0)
        assert len(step["stderr"]) <= mod._STDERR_CAP

    def test_credential_straddling_truncation_boundary_is_still_redacted(self, monkeypatch, caplog):
        """A URL credential whose ``@`` anchor sits past the display cap.

        Truncating first would split ``://user:pass@host`` so the trailing
        ``@`` is gone; the regex no longer matches, leaking the password
        fragment. Redacting before truncation eliminates this.
        """
        # Place the URL so its @ lands past _STDERR_CAP.
        padding = "x" * 1982
        url = "http://admin:LEAKED_SECRET_VALUE@proxy.corp.example.com"
        stderr = padding + url
        assert stderr.index("@") > mod._STDERR_CAP, "test setup: @ must be past cap"

        monkeypatch.setattr(mod, "_run", lambda argv, timeout: (1, "", stderr))

        with caplog.at_level("WARNING"):
            step = mod._step("npm-install-global", ["npm", "install"], 1.0)

        # The secret must not survive in either the returned detail or the log.
        assert "LEAKED_SECRET_VALUE" not in step["stderr"]
        assert "LEAKED_SECRET_VALUE" not in caplog.text
        # The redaction marker proves the credential was caught (it may be
        # truncated itself if it lands at the cap boundary, so check that
        # the raw password text between ``:`` and ``@`` is gone).
        assert "LEAKED_SECRET" not in step["stderr"]

    @pytest.mark.parametrize(
        "secret_line",
        [
            "//registry.npmjs.org/:_authToken=npm_abc123secretXYZ",
            "_password=c3VwZXJzZWNyZXQ=",
            "NPM_TOKEN=ghp_1234567890abcdefABCDEF1234567890abcd",
            "http://deploy:s3cr3tP@ss@registry.internal.example.com/pkg",
        ],
        ids=["authToken", "password", "env-token", "url-creds"],
    )
    def test_npm_credential_shapes_are_all_redacted(self, monkeypatch, secret_line):
        """Every npm credential shape is caught regardless of position."""
        stderr = f"npm ERR! 404 Not Found\n{secret_line}\nnpm ERR! done"
        monkeypatch.setattr(mod, "_run", lambda argv, timeout: (1, "", stderr))

        step = mod._step("npm-install-global", ["npm", "install"], 1.0)

        # Extract the actual secret value (the part after = or between : and @)
        # and confirm it does not survive.
        assert "[REDACTED]" in step["stderr"]
        # None of the raw secret portions should appear.
        for fragment in (
            "npm_abc123secretXYZ",
            "c3VwZXJzZWNyZXQ",
            "ghp_1234567890abcdefABCDEF1234567890abcd",
            "s3cr3tP@ss",
        ):
            if fragment in secret_line:
                assert fragment not in step["stderr"]

    def test_redaction_timing_scales_linearly(self):
        """Doubling the input must not more than triple the runtime.

        An absolute-duration assertion is fragile across CI environments
        (coverage overhead, CPU contention). A bounded RATIO between two
        input sizes is stable: quadratic or exponential growth (ReDoS)
        doubles the ratio on each doubling of input, while linear growth
        keeps it near 2.0.

        Two measurement details keep the ratio itself stable on a shared CI
        runner, matching what ``TestIsDeniedReDoSResistance`` already does
        for the same class of assertion:

        * ``thread_time``, not ``perf_counter``: wall-clock bills this test
          for however long the OS gave the core to the sibling pytest-xdist
          workers, and that noise lands unevenly across the two samples.
          Redaction is single-threaded pure-regex work, so per-thread CPU is
          its complete cost — a genuinely catastrophic pattern inflates it
          identically.
        * Best-of-3 per size: this is a floor measurement, and scheduler
          noise only ever ADDS, so the minimum is the closest estimate of
          the true cost. A single sample per size (what this test used
          before) let one unlucky small-input reading — the denominator —
          push the ratio over the limit on an otherwise-healthy matcher,
          which is how this failed CI intermittently at ~3.1-3.2x.

        Neither change weakens the guarantee: the bound stays at 3.0x, and
        quadratic growth still lands at >=4x on every sample.
        """
        import time

        def cost(text: str) -> float:
            """CPU consumed by THIS thread redacting *text*."""
            start = time.thread_time()
            mod._redact(text)
            return time.thread_time() - start

        # Adversarial: all chars match the env-var prefix class [A-Z0-9_],
        # the shape that triggered the original catastrophic backtracking
        # before the {0,40} bound was added.
        small = "A" * 25_000
        large = "A" * 50_000

        # Warm up (JIT, import overhead).
        mod._redact(small)

        t_small = min(cost(small) for _ in range(3))
        t_large = min(cost(large) for _ in range(3))

        # Linear growth ⇒ ratio ≈ 2.0; allow up to 3.0 for noise.
        # ReDoS (quadratic+) yields ratio ≥ 4.0 reliably.
        ratio = t_large / max(t_small, 1e-9)
        assert ratio < 3.0, (
            f"Redaction scaled super-linearly: {t_large:.4f}s / "
            f"{t_small:.4f}s = {ratio:.1f}x (limit 3.0x)"
        )


class TestCliEnvIsPublic:
    """The Node-augmented env helper is importable by view.py and other callers."""

    def test_cli_env_is_importable_by_name(self) -> None:
        from kiro_crew.browser_cli.install import cli_env

        assert callable(cli_env)

    def test_cli_env_augments_path_from_node_augmented_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(mod, "augmented_path", lambda base: base)
        monkeypatch.setattr(mod, "node_augmented_path", lambda base: f"/nvm/bin:{base}")
        monkeypatch.setenv("PATH", "/usr/local/bin")

        env = mod.cli_env()

        assert env["PATH"] == "/nvm/bin:/usr/local/bin"


@pytest.mark.skipif(
    os.name == "nt",
    reason=(
        "POSIX-only three times over: ntpath.expanduser reads USERPROFILE and ignores "
        "HOME, so the fake profile is never consulted; shutil.which needs a PATHEXT "
        "match, which an extension-less wrapper has not; and the exec bit does not "
        "carry. The Windows layout is ~\\.local\\bin\\playwright-cli.cmd, which the "
        "same augmented_path entry covers -- untestable here, not unhandled."
    ),
)
def test_the_cli_is_found_where_the_standalone_installer_puts_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`npm install -g` leaves the wrapper beside npm, but `playwright-cli.sh`
    writes it to `~/.local/bin` -- a directory a systemd/launchd/service-manager
    gateway does not inherit on $PATH, and one `node_bin_dirs()` never reports
    because it holds no `node`. Searched over the bare PATH, a SUCCESSFUL
    standalone install would keep reading as "not installed": the panel would go
    on offering the command the user just ran, with nothing anywhere reporting an
    error."""
    local_bin = tmp_path / ".local" / "bin"
    local_bin.mkdir(parents=True)
    wrapper = local_bin / mod.CLI_BIN
    wrapper.write_text("#!/bin/sh\n", encoding="utf-8")
    wrapper.chmod(0o755)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", "/usr/bin")

    assert mod.cli_path() == str(wrapper)


def test_the_standalone_command_writes_no_fixed_name_into_the_working_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The operator pastes this into whatever shell is open, so the download
    destination is a directory the command does not own. A fixed
    `playwright-cli.sh` in the working directory would be truncated -- their own
    copy, or an unrelated file that merely shares the name."""
    monkeypatch.setattr(mod.os, "name", "posix")
    posix = mod._standalone_install_command()
    assert "mktemp -d" in posix
    assert "-fsSLO" not in posix, "-O derives the name from the URL, into the cwd"
    assert 'sh "$_pwcli_dir/playwright-cli.sh"' in posix
    assert "d=$(mktemp" not in posix, "must not clobber a common scratch variable"

    monkeypatch.setattr(mod.os, "name", "nt")
    windows = mod._standalone_install_command()
    assert "$env:TEMP" in windows
    assert "NewGuid" in windows
    assert "-OutFile $p" in windows
    assert ".\\playwright-cli.ps1" not in windows


def test_detect_offers_the_os_appropriate_standalone_installer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The panel's Node-blocked state used to end at "Download Node.js", which is
    the one thing the operator it describes often cannot do -- no admin rights, or a
    registry that needs a login. `detect()` therefore carries the standalone
    installer command, and composes it HERE because only the gateway knows which OS
    it runs on: the dashboard may be open on a different machine, and offering two
    commands to choose between puts that guess on the user.

    It is also the only place it can live. A shell command must not enter the i18n
    catalogs -- the pseudolocale accents every Latin character, which would corrupt
    the URL -- and the dashboard's untranslated-literal gate forbids holding it in
    the component.
    """
    monkeypatch.setattr(mod.os, "name", "posix")
    posix = mod._standalone_install_command()
    assert "playwright-cli.sh" in posix
    assert "powershell" not in posix
    # Download-then-run rather than a pipe into a shell: a machine locked down
    # enough to need this usually forbids piping the network into `sh`.
    assert "| sh" not in posix
    assert "curl -fsSL" in posix

    monkeypatch.setattr(mod.os, "name", "nt")
    windows = mod._standalone_install_command()
    assert "playwright-cli.ps1" in windows
    assert "playwright-cli.sh" not in windows

    # `detect()` is exercised under the REAL platform: with `os.name` patched to
    # "nt", pathlib refuses to build a WindowsPath on Linux and the call dies
    # before the payload exists. The Windows branch above is the helper's job.
    monkeypatch.undo()
    assert mod.detect()["standalone_install"] == mod._standalone_install_command()


# A trimmed real browsers.json, same shape playwright-core ships. chromium and
# chromium-headless-shell share a revision; firefox/webkit differ, which is what
# lets a test prove the match is per-engine and not a single global number.
_MANIFEST = {
    "comment": "Do not edit this file",
    "browsers": [
        {"name": "chromium", "revision": "1232", "installByDefault": True},
        {"name": "chromium-headless-shell", "revision": "1232", "installByDefault": True},
        {"name": "firefox", "revision": "1534", "installByDefault": True},
        {"name": "webkit", "revision": "2327", "installByDefault": True},
        {"name": "ffmpeg", "revision": "1011", "installByDefault": True},
    ],
}


def _write_manifest(node_modules: Path, data: object) -> Path:
    """Write *data* as ``<node_modules>/playwright-core/browsers.json``."""
    core = node_modules / "playwright-core"
    core.mkdir(parents=True, exist_ok=True)
    path = core / "browsers.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _install_root(root: Path, data: object) -> str:
    """Model an `npm install -g` tree under *root*; return the CLI entry path.

    ``<root>/lib/node_modules/@playwright/cli/playwright-cli.js`` with
    ``playwright-core`` hoisted beside it in the same ``node_modules``. That is
    the layout the manifest resolution anchors on, and the entry point is a real
    on-disk file so ``Path(cli).resolve()`` canonicalises the same tree the
    manifest was written into.
    """
    node_modules = root / "lib" / "node_modules"
    _write_manifest(node_modules, data)
    package = node_modules / "@playwright" / "cli"
    package.mkdir(parents=True, exist_ok=True)
    entry = package / "playwright-cli.js"
    entry.write_text("// entry\n", encoding="utf-8")
    return str(entry)


class TestRequiredRevisionMatch:
    """`browsers_present` compares cached revision to the CLI's required one.

    The regression that must never return: a stale cache dir whose engine prefix
    matches but whose revision is wrong reads as present, `browser_ok` goes true,
    the launch fails "Browser ... is not installed", and the panel never offers
    the download that fixes it.
    """

    def test_matching_revision_reads_ready(
        self, monkeypatch: pytest.MonkeyPatch, isolated_browser_cache: Path, tmp_path: Path
    ) -> None:
        root = tmp_path / "cli-root"
        launcher = _install_root(root, _MANIFEST)
        monkeypatch.setattr(mod, "cli_path", lambda: launcher)
        monkeypatch.setattr(
            mod, "_required_revisions", _REAL_REQUIRED_REVISIONS
        )  # use the real one
        (isolated_browser_cache / "chromium-1232").mkdir()

        assert mod.browsers_present()["chromium"] is True
        assert mod._browser_present() is True
        assert mod.detect()["browser_ok"] is True

    def test_stale_revision_is_not_ready(
        self, monkeypatch: pytest.MonkeyPatch, isolated_browser_cache: Path, tmp_path: Path
    ) -> None:
        """THE regression. chromium-1208 is present, 1232 is required."""
        root = tmp_path / "cli-root"
        launcher = _install_root(root, _MANIFEST)
        monkeypatch.setattr(mod, "cli_path", lambda: launcher)
        monkeypatch.setattr(mod, "_required_revisions", _REAL_REQUIRED_REVISIONS)
        (isolated_browser_cache / "chromium-1208").mkdir()

        assert mod.browsers_present()["chromium"] is False
        assert mod._browser_present() is False
        # The gate is honest, so the panel can offer the download.
        assert mod.detect()["browser_ok"] is False

    def test_headless_shell_underscore_variant_does_not_satisfy_chromium(
        self, monkeypatch: pytest.MonkeyPatch, isolated_browser_cache: Path, tmp_path: Path
    ) -> None:
        """`chromium_headless_shell-1232` starts with "chromium", so the old
        prefix match counted it as a Chromium build. It is a different artifact;
        only a real `chromium-<rev>` dir satisfies the attach engine."""
        root = tmp_path / "cli-root"
        launcher = _install_root(root, _MANIFEST)
        monkeypatch.setattr(mod, "cli_path", lambda: launcher)
        monkeypatch.setattr(mod, "_required_revisions", _REAL_REQUIRED_REVISIONS)
        (isolated_browser_cache / "chromium_headless_shell-1232").mkdir()

        assert mod.browsers_present()["chromium"] is False
        assert mod._browser_present() is False

    def test_cache_dir_name_is_the_hyphenated_revision_form(self) -> None:
        """One name per engine-revision pair, and never an underscore variant.

        The only caller passes engines from `BROWSER_ENGINES`, none of which
        contains a hyphen, so an underscored form of the same name could not
        match any directory -- and accepting one would let
        `chromium_headless_shell-<rev>` satisfy `chromium`, which is the very
        false positive this gate exists to reject.
        """
        assert mod._cache_dir_name_for("chromium", "1232") == "chromium-1232"
        assert "_" not in mod._cache_dir_name_for("chromium", "1232")

    def test_an_underscored_cache_dir_does_not_satisfy_the_engine(
        self, monkeypatch: pytest.MonkeyPatch, isolated_browser_cache: Path, tmp_path: Path
    ) -> None:
        """The same rejection asserted through `browsers_present`, not the helper.

        Testing only `_cache_dir_name_for` would still pass if a future change
        re-added an underscore fallback in the caller instead of the helper, which
        is where the original prefix match lived.
        """
        launcher = _install_root(tmp_path / "cli-root", _MANIFEST)
        monkeypatch.setattr(mod, "cli_path", lambda: launcher)
        monkeypatch.setattr(mod, "_required_revisions", _REAL_REQUIRED_REVISIONS)
        (isolated_browser_cache / "chromium_1232").mkdir()

        assert mod.browsers_present()["chromium"] is False
        assert mod._browser_present() is False

    def test_each_engine_matched_against_its_own_revision(
        self, monkeypatch: pytest.MonkeyPatch, isolated_browser_cache: Path, tmp_path: Path
    ) -> None:
        """firefox at its required 1534 is ready; webkit at a wrong revision is
        not -- proving the check is per-engine, not one shared number."""
        root = tmp_path / "cli-root"
        launcher = _install_root(root, _MANIFEST)
        monkeypatch.setattr(mod, "cli_path", lambda: launcher)
        monkeypatch.setattr(mod, "_required_revisions", _REAL_REQUIRED_REVISIONS)
        (isolated_browser_cache / "firefox-1534").mkdir()
        (isolated_browser_cache / "webkit-9999").mkdir()

        present = mod.browsers_present()
        assert present["firefox"] is True
        assert present["webkit"] is False

    @pytest.mark.parametrize("required_rev", ["1232", "1300", "1208", "9999"])
    def test_ready_iff_cached_revision_equals_required(
        self,
        monkeypatch: pytest.MonkeyPatch,
        isolated_browser_cache: Path,
        tmp_path: Path,
        required_rev: str,
    ) -> None:
        """Property: for ANY required revision, a single cached chromium-1232 dir
        reads ready exactly when 1232 is what is required -- never on a mismatch.

        This is the property the prefix match violated: it answered "ready" for
        every one of these required revisions. A mutation reintroducing
        `startswith` fails on the three mismatched cases below.
        """
        root = tmp_path / "cli-root"
        launcher = _install_root(
            root,
            {"browsers": [{"name": "chromium", "revision": required_rev}]},
        )
        monkeypatch.setattr(mod, "cli_path", lambda: launcher)
        monkeypatch.setattr(mod, "_required_revisions", _REAL_REQUIRED_REVISIONS)
        (isolated_browser_cache / "chromium-1232").mkdir()

        assert mod.browsers_present()["chromium"] is (required_rev == "1232")


class TestRevisionDegradation:
    """When the required revision cannot be determined, a working browser must
    not be reported broken. Missing metadata is an unknown, not a stale cache."""

    def test_absent_manifest_falls_back_to_presence_only(
        self, monkeypatch: pytest.MonkeyPatch, isolated_browser_cache: Path
    ) -> None:
        """No resolvable manifest -> today's prefix behaviour, so any chromium-*
        dir still reads ready rather than flipping to broken."""
        monkeypatch.setattr(mod, "_browsers_manifest_path", lambda: None)
        monkeypatch.setattr(mod, "_required_revisions", _REAL_REQUIRED_REVISIONS)
        (isolated_browser_cache / "chromium-1208").mkdir()

        assert mod.browsers_present()["chromium"] is True
        assert mod._browser_present() is True

    def test_unreadable_manifest_falls_back_to_presence_only(
        self, monkeypatch: pytest.MonkeyPatch, isolated_browser_cache: Path, tmp_path: Path
    ) -> None:
        """A manifest that is not valid JSON is treated as unknown, not fatal."""
        root = tmp_path / "cli-root"
        core = root / "node_modules" / "playwright-core"
        core.mkdir(parents=True)
        (core / "browsers.json").write_text("{ not json", encoding="utf-8")
        bindir = root / "bin"
        bindir.mkdir()
        launcher = bindir / "playwright-cli"
        launcher.write_text("#!/bin/sh\n", encoding="utf-8")
        monkeypatch.setattr(mod, "cli_path", lambda: str(launcher))
        monkeypatch.setattr(mod, "_required_revisions", _REAL_REQUIRED_REVISIONS)
        (isolated_browser_cache / "chromium-1208").mkdir()

        assert _REAL_REQUIRED_REVISIONS() is None
        assert mod.browsers_present()["chromium"] is True

    def test_manifest_missing_an_engine_degrades_only_that_engine(
        self, monkeypatch: pytest.MonkeyPatch, isolated_browser_cache: Path, tmp_path: Path
    ) -> None:
        """A manifest that lists chromium but not webkit: chromium is matched by
        revision, webkit degrades to presence-only rather than reading broken."""
        root = tmp_path / "cli-root"
        launcher = _install_root(root, {"browsers": [{"name": "chromium", "revision": "1232"}]})
        monkeypatch.setattr(mod, "cli_path", lambda: launcher)
        monkeypatch.setattr(mod, "_required_revisions", _REAL_REQUIRED_REVISIONS)
        (isolated_browser_cache / "chromium-1208").mkdir()  # stale -> not ready
        (isolated_browser_cache / "webkit-2248").mkdir()  # no required rev -> present

        present = mod.browsers_present()
        assert present["chromium"] is False
        assert present["webkit"] is True

    def test_malformed_rows_are_skipped_not_fatal(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        root = tmp_path / "cli-root"
        launcher = _install_root(
            root,
            {
                "browsers": [
                    "not-a-dict",
                    {"name": "chromium"},  # no revision
                    {"revision": "1"},  # no name
                    {"name": "webkit", "revision": 2327},  # int revision -> skipped
                    {"name": "firefox", "revision": "1534"},
                ]
            },
        )
        monkeypatch.setattr(mod, "cli_path", lambda: launcher)
        monkeypatch.setattr(mod, "_required_revisions", _REAL_REQUIRED_REVISIONS)

        rev = _REAL_REQUIRED_REVISIONS()
        # playwright-core ships string revisions, so a non-string is a malformed
        # row like any other rather than a value to coerce. Skipping it leaves the
        # engine without a required revision, which degrades to presence-only for
        # that engine -- the safe direction.
        assert rev == {"firefox": "1534"}

    def test_manifest_without_browsers_list_is_none(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        root = tmp_path / "cli-root"
        launcher = _install_root(root, {"comment": "empty"})
        monkeypatch.setattr(mod, "cli_path", lambda: launcher)
        monkeypatch.setattr(mod, "_required_revisions", _REAL_REQUIRED_REVISIONS)

        assert _REAL_REQUIRED_REVISIONS() is None


class TestManifestResolution:
    """The manifest is attributed to a `@playwright/cli` package, never searched for.

    Anchoring is the correctness property. An unbounded walk up from the launcher
    passes through `$HOME` on the standalone layout, where one unrelated
    `~/node_modules/playwright-core` would supply a revision from a DIFFERENT
    install -- reporting a WORKING browser broken, and continuing to after the
    download the panel offers, because the gate keeps reading the foreign file.
    """

    def test_npm_global_hoisted_sibling_layout(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """`playwright-core` hoisted beside `@playwright/cli` in one node_modules."""
        root = tmp_path / "cli-root"
        entry = _install_root(root, _MANIFEST)
        monkeypatch.setattr(mod, "cli_path", lambda: entry)

        assert mod._browsers_manifest_path() == (
            root / "lib" / "node_modules" / "playwright-core" / "browsers.json"
        )

    def test_nested_playwright_core_layout(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """npm nests a conflicting version under the package's own node_modules.

        The nested copy is the one that serves this CLI, so it wins over a
        hoisted sibling.
        """
        root = tmp_path / "cli-root"
        entry = Path(_install_root(root, {"browsers": [{"name": "chromium", "revision": "1"}]}))
        nested = _write_manifest(entry.parent / "node_modules", _MANIFEST)
        monkeypatch.setattr(mod, "cli_path", lambda: str(entry))

        assert mod._browsers_manifest_path() == nested

    def test_npm_global_symlinked_bin_resolves_into_the_package(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """`npm install -g` leaves a symlink; resolving it lands in the tree."""
        root = tmp_path / "cli-root"
        entry = _install_root(root, _MANIFEST)
        bindir = tmp_path / "bin"
        bindir.mkdir()
        link = bindir / "playwright-cli"
        try:
            link.symlink_to(entry)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks not supported on this host")
        monkeypatch.setattr(mod, "cli_path", lambda: str(link))

        assert mod._browsers_manifest_path() == (
            root / "lib" / "node_modules" / "playwright-core" / "browsers.json"
        )

    def test_standalone_wrapper_resolves_via_its_known_prefix(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The standalone installer generates a WRAPPER SCRIPT, not a symlink.

        The package tree is therefore not an ancestor of the launcher on PATH, so
        no walk up from it can reach the manifest. The prefix is known, so it is
        probed by path -- without which this install shape could never read a
        revision and would silently keep the presence-only false positives.
        """
        prefix = tmp_path / "standalone"
        _install_root(prefix, _MANIFEST)
        monkeypatch.setenv("KIROCREW_PLAYWRIGHT_CLI_HOME", str(prefix))
        wrapper = tmp_path / "elsewhere" / "bin" / "playwright-cli"
        wrapper.parent.mkdir(parents=True)
        wrapper.write_text("#!/bin/sh\nexec node ...\n", encoding="utf-8")
        monkeypatch.setattr(mod, "cli_path", lambda: str(wrapper))

        assert mod._browsers_manifest_path() == (
            prefix / "lib" / "node_modules" / "playwright-core" / "browsers.json"
        )

    def test_windows_npm_global_cmd_wrapper_finds_the_sibling_package(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """`npm install -g` writes a .cmd BATCH WRAPPER on Windows, not a symlink.

        It resolves to itself, so nothing in its ancestry is the package dir and
        the package sits at <prefix>/node_modules/@playwright/cli beside it. Left
        unreachable, this shape reads no revision and silently falls back to the
        presence-only match the gate exists to replace.
        """
        prefix = tmp_path / "npm-prefix"
        node_modules = prefix / "node_modules"
        manifest = _write_manifest(node_modules, _MANIFEST)
        package = node_modules / "@playwright" / "cli"
        package.mkdir(parents=True)
        (package / "playwright-cli.js").write_text("// entry\n", encoding="utf-8")
        wrapper = prefix / "playwright-cli.cmd"
        wrapper.write_text("@echo off\r\nnode ...\r\n", encoding="utf-8")
        monkeypatch.setenv("KIROCREW_PLAYWRIGHT_CLI_HOME", str(tmp_path / "absent-prefix"))
        monkeypatch.setattr(mod, "cli_path", lambda: str(wrapper))

        assert mod._browsers_manifest_path() == manifest

    def test_posix_bin_wrapper_finds_the_lib_node_modules_package(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A non-symlink launcher at <prefix>/bin resolves to itself too."""
        prefix = tmp_path / "posix-prefix"
        manifest_root = prefix / "lib" / "node_modules"
        manifest = _write_manifest(manifest_root, _MANIFEST)
        package = manifest_root / "@playwright" / "cli"
        package.mkdir(parents=True)
        wrapper = prefix / "bin" / "playwright-cli"
        wrapper.parent.mkdir(parents=True)
        wrapper.write_text("#!/bin/sh\nexec node ...\n", encoding="utf-8")
        monkeypatch.setenv("KIROCREW_PLAYWRIGHT_CLI_HOME", str(tmp_path / "absent-prefix"))
        monkeypatch.setattr(mod, "cli_path", lambda: str(wrapper))

        assert mod._browsers_manifest_path() == manifest

    def test_standalone_windows_prefix_without_lib_resolves(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """`npm --global --prefix` writes <prefix>/node_modules on Windows.

        The POSIX form is <prefix>/lib/node_modules, so both are probed.
        """
        prefix = tmp_path / "standalone-win"
        node_modules = prefix / "node_modules"
        manifest = _write_manifest(node_modules, _MANIFEST)
        (node_modules / "@playwright" / "cli").mkdir(parents=True)
        monkeypatch.setenv("KIROCREW_PLAYWRIGHT_CLI_HOME", str(prefix))
        monkeypatch.setattr(mod, "cli_path", lambda: None)

        assert mod._browsers_manifest_path() == manifest

    def test_a_foreign_manifest_up_the_tree_is_never_adopted(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """THE false-broken regression, and the inverse of the stale-cache one.

        A stray `playwright-core` from an unrelated `npm i playwright` sits in an
        ANCESTOR of the launcher, with no `@playwright/cli` beside it. Adopting it
        would hand the gate a revision from a different install and flip a working
        browser to reported-broken. Unattributable means None, which takes the
        documented presence-only fallback instead.
        """
        home = tmp_path / "home"
        _write_manifest(
            home / "node_modules", {"browsers": [{"name": "chromium", "revision": "9"}]}
        )
        monkeypatch.setenv("KIROCREW_PLAYWRIGHT_CLI_HOME", str(tmp_path / "absent-prefix"))
        wrapper = home / "bin" / "playwright-cli"
        wrapper.parent.mkdir(parents=True)
        wrapper.write_text("#!/bin/sh\n", encoding="utf-8")
        monkeypatch.setattr(mod, "cli_path", lambda: str(wrapper))

        assert mod._browsers_manifest_path() is None
        assert _REAL_REQUIRED_REVISIONS() is None

    def test_a_working_browser_is_not_reported_broken_by_a_foreign_manifest(
        self, monkeypatch: pytest.MonkeyPatch, isolated_browser_cache: Path, tmp_path: Path
    ) -> None:
        """The invariant the anchoring protects, asserted end to end."""
        home = tmp_path / "home"
        _write_manifest(
            home / "node_modules", {"browsers": [{"name": "chromium", "revision": "9999"}]}
        )
        monkeypatch.setenv("KIROCREW_PLAYWRIGHT_CLI_HOME", str(tmp_path / "absent-prefix"))
        wrapper = home / "bin" / "playwright-cli"
        wrapper.parent.mkdir(parents=True)
        wrapper.write_text("#!/bin/sh\n", encoding="utf-8")
        monkeypatch.setattr(mod, "cli_path", lambda: str(wrapper))
        monkeypatch.setattr(mod, "_required_revisions", _REAL_REQUIRED_REVISIONS)
        (isolated_browser_cache / "chromium-1232").mkdir()

        assert mod.browsers_present()["chromium"] is True

    def test_no_cli_means_no_manifest(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(mod, "cli_path", lambda: None)
        monkeypatch.setenv("KIROCREW_PLAYWRIGHT_CLI_HOME", "/nonexistent-prefix")
        assert mod._browsers_manifest_path() is None
        assert _REAL_REQUIRED_REVISIONS() is None
