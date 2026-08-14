"""Detection, install sequencing, and the presence-is-consent gate."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from kiro_crew import platform_compat
from kiro_crew.browser_cli import install as mod


@pytest.fixture(autouse=True)
def isolated_browser_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep ``browser_ok`` off the developer's real Playwright cache."""
    cache = tmp_path / "ms-playwright"
    cache.mkdir()
    monkeypatch.setattr(mod, "_browsers_cache_dir", lambda: cache)
    return cache


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
    monkeypatch.setattr(platform_compat, "IS_LINUX", False)
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


def test_install_adds_with_deps_on_linux_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """``--with-deps`` drives the system package manager, so it is Linux-only."""
    monkeypatch.setattr(platform_compat, "IS_LINUX", True)
    linux_calls = _wire(monkeypatch, {"npm": "/n/npm", "playwright-cli": "/n/pw"})
    mod.install()
    assert ["/n/pw", "install-browser", "--with-deps"] in linux_calls

    monkeypatch.setattr(platform_compat, "IS_LINUX", False)
    other_calls = _wire(monkeypatch, {"npm": "/n/npm", "playwright-cli": "/n/pw"})
    mod.install()
    assert ["/n/pw", "install-browser"] in other_calls
    assert all("--with-deps" not in argv for argv in other_calls)


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
    monkeypatch.setattr(platform_compat, "IS_LINUX", False)
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


def test_cli_env_puts_node_dirs_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """A global npm bin dir the gateway never had on PATH must still be found."""
    monkeypatch.setattr(mod, "node_augmented_path", lambda base: f"/node/bin:{base}")
    monkeypatch.setenv("PATH", "/usr/bin")

    assert mod.cli_env()["PATH"] == "/node/bin:/usr/bin"


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
        monkeypatch.setattr(mod, "cli_path", lambda: fake_cli)
        seen: list[list[str]] = []

        def _fake_step(name, argv, timeout):
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
        """
        import time

        # Adversarial: all chars match the env-var prefix class [A-Z0-9_],
        # the shape that triggered the original catastrophic backtracking
        # before the {0,40} bound was added.
        small = "A" * 25_000
        large = "A" * 50_000

        # Warm up (JIT, import overhead).
        mod._redact(small)

        start = time.perf_counter()
        mod._redact(small)
        t_small = time.perf_counter() - start

        start = time.perf_counter()
        mod._redact(large)
        t_large = time.perf_counter() - start

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
