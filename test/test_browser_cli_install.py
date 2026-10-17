"""Detection, install sequencing, and the capability-availability probe.

This file also holds the per-engine downloads, failure-detail redaction, the CLI
environment and which launcher path is trusted. Browser revisions and status live in
``test_browser_cli_install_browser_revisions.py``, the gateway command in
``test_browser_cli_install_gateway_command.py``, stage reporting, the installer's
process tree and the standalone installer command ``detect()`` offers in
``test_browser_cli_install_run_control.py``, and the shared autouse pins and fakes in
``browser_cli_install_helpers.py``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from browser_cli_install_helpers import _clear_node_bin_dir_caches  # noqa: F401
from browser_cli_install_helpers import _default_no_os_deps  # noqa: F401
from browser_cli_install_helpers import isolated_browser_cache_fixture  # noqa: F401
from browser_cli_install_helpers import _complete, _wire

from kiro_crew import env as env_mod
from kiro_crew.browser_cli import install as mod


def test_detect_reports_absent_when_binary_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    _wire(monkeypatch, {"node": "/n/node"}, {"/n/node": (0, "v22.1.0", "")})

    d = mod.detect()

    assert d["installed"] is False
    assert d["cli_path"] is None
    assert d["cli_version"] is None
    # Node being fine must not be reported as the CLI being present.
    assert d["node_ok"] is True


def test_detect_reports_version_when_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = tmp_path / "node_modules" / "@playwright" / "cli"
    package.mkdir(parents=True)
    entry = package / "playwright-cli.js"
    entry.write_text("", encoding="utf-8")
    (package / "package.json").write_text(
        json.dumps({"name": "@playwright/cli", "version": "0.1.18"}),
        encoding="utf-8",
    )
    calls = _wire(
        monkeypatch,
        {"node": "/n/node", "playwright-cli": "/n/playwright-cli"},
        {"/n/node": (0, "v22.1.0", "")},
    )
    monkeypatch.setattr(mod, "cli_command", lambda cli=None: ["/n/node", str(entry)])

    d = mod.detect()

    assert d["installed"] is True
    assert d["cli_path"] == "/n/playwright-cli"
    assert d["cli_version"] == "0.1.18"
    assert calls == [["/n/node", "--version"]]


def test_installed_cli_version_reads_package_metadata_each_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = tmp_path / "node_modules" / "@playwright" / "cli"
    package.mkdir(parents=True)
    entry = package / "playwright-cli.js"
    entry.write_text("", encoding="utf-8")
    manifest = package / "package.json"
    manifest.write_text(json.dumps({"version": "0.1.21"}), encoding="utf-8")
    real_read_text = Path.read_text
    reads: list[Path] = []

    def _read_text(path: Path, *args, **kwargs) -> str:
        if path == manifest:
            reads.append(path)
        return real_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _read_text)
    command = ["/n/node", str(entry)]

    assert mod.installed_cli_version(command) == "0.1.21"
    manifest.write_text(json.dumps({"version": "0.1.22"}), encoding="utf-8")
    assert mod.installed_cli_version(command) == "0.1.22"
    assert reads == [manifest, manifest]


def test_detect_reports_a_launcher_without_a_safe_runtime_as_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _wire(
        monkeypatch,
        {"node": "/n/node", "playwright-cli": "/n/playwright-cli"},
        {"/n/node": (0, "v22.1.0", "")},
    )
    monkeypatch.setattr(mod, "cli_command", lambda cli=None: None)

    detected = mod.detect()

    assert detected["installed"] is False
    assert detected["cli_path"] is None
    assert detected["cli_version"] is None


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
    _complete(isolated_browser_cache / "firefox-1489")
    assert mod.detect()["browser_ok"] is False

    _complete(isolated_browser_cache / "chromium-1200")
    assert mod.detect()["browser_ok"] is True


def test_available_is_false_without_the_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    _wire(monkeypatch, {"node": "/n/node"}, {"/n/node": (0, "v22.1.0", "")})

    assert mod.available() is False


def test_available_is_presence_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Presence reports availability even with a broken Node or missing browser.

    Reporting a repairable environment as absent would send the operator to the
    wrong fix. Shell approval is a separate decision made by the runner.
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
    """Availability resolves a vetted launcher and no consent flag.

    An empty data home means no managed launcher, not "browser disabled". Approval
    remains outside this module and is decided by the ordinary shell ladder.
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


def test_install_refuses_a_linked_managed_prefix_before_npm_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    crew = tmp_path / "crew"
    target = tmp_path / "agent-chosen-target"
    crew.mkdir()
    target.mkdir()
    (crew / "playwright-cli").symlink_to(target, target_is_directory=True)
    monkeypatch.setattr(
        Path,
        "is_dir",
        lambda self: (_ for _ in ()).throw(AssertionError("pre-pin type probe followed the name")),
    )
    calls: list[list[str]] = []
    monkeypatch.setattr(mod, "config_dir", lambda: crew)
    monkeypatch.setattr(
        mod,
        "find_node_tool",
        lambda name, base_path=None: "/n/npm" if name == "npm" else "/n/node",
    )
    monkeypatch.setattr(
        mod,
        "_run",
        lambda argv, timeout: (calls.append(list(argv)), (0, "", ""))[1],
    )

    result = mod.install()

    assert result["ok"] is False
    assert calls == []
    assert list(target.iterdir()) == []
    assert "real directory" in result["steps"][0]["stderr"]


def test_install_stops_before_npm_when_the_platform_pin_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    crew = tmp_path / "crew"
    crew.mkdir()
    calls: list[list[str]] = []
    monkeypatch.setattr(mod, "config_dir", lambda: crew)
    monkeypatch.setattr(
        mod,
        "find_node_tool",
        lambda name, base_path=None: "/n/npm" if name == "npm" else "/n/node",
    )
    monkeypatch.setattr(
        mod.platform_compat,
        "pin_directory",
        lambda path: (_ for _ in ()).throw(OSError("reparse point")),
    )
    monkeypatch.setattr(
        mod,
        "_run",
        lambda argv, timeout: (calls.append(list(argv)), (0, "", ""))[1],
    )

    result = mod.install()

    assert result["ok"] is False
    assert calls == []
    assert "real directory" in result["steps"][0]["stderr"]


def test_install_runs_all_steps_and_scopes_browser_to_chromium(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    crew = tmp_path / "crew"
    monkeypatch.setattr(mod, "config_dir", lambda: crew)
    calls = _wire(monkeypatch, {"npm": "/n/npm", "playwright-cli": "/n/playwright-cli"})

    result = mod.install()

    assert result["ok"] is True
    assert [s["name"] for s in result["steps"]] == [
        "npm-install-global",
        "stage-node",
        "install-browser",
        "install-skills",
    ]
    assert calls[0] == [
        "/n/npm",
        "install",
        "-g",
        "--prefix",
        str(crew / "playwright-cli"),
        "@playwright/cli@latest",
    ]
    # Omitting the argument installs every engine. Optional WebKit dependencies
    # must not veto a baseline Chromium install on a host where Chromium works.
    assert calls[1] == ["/n/playwright-cli", "install-browser", "chromium"]
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
    rather than on "is Linux". The engine is always named explicitly."""
    monkeypatch.setattr(mod.os_deps, "with_deps_supported", lambda: True)
    apt_calls = _wire(monkeypatch, {"npm": "/n/npm", "playwright-cli": "/n/pw"})
    mod.install()
    assert ["/n/pw", "install-browser", "chromium", "--with-deps"] in apt_calls

    monkeypatch.setattr(mod.os_deps, "with_deps_supported", lambda: False)
    other_calls = _wire(monkeypatch, {"npm": "/n/npm", "playwright-cli": "/n/pw"})
    mod.install()
    assert ["/n/pw", "install-browser", "chromium"] in other_calls
    assert all("--with-deps" not in argv for argv in other_calls)


def test_install_falls_back_without_deps_when_the_package_step_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refused ``apt-get`` must not cost the operator the browser.

    ``--with-deps`` shells out to ``apt-get`` as root, sudo policy refuses it,
    and because the flag and the download are one CLI invocation the download
    failed too -- even though it needs no privilege at all. The remedy rides only
    on the retry, the attempt a human has to act on.
    """
    monkeypatch.setattr(mod.os_deps, "with_deps_supported", lambda: True)
    monkeypatch.setattr(
        mod.os_deps, "missing_deps_hint", lambda engine="chromium": "run this: sudo apt-get ..."
    )
    calls = _wire(monkeypatch, {"npm": "/n/npm", "playwright-cli": "/n/pw"})

    def fake_run(argv: list[str], timeout: float, **_kw: Any) -> tuple[int, str, str]:
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
        "stage-node",
        "install-browser",
        "install-browser-no-deps",
        "install-skills",
    ]
    # The refused attempt stays visible rather than being swallowed...
    assert result["steps"][2]["ok"] is False
    assert result["steps"][2]["hint"] == ""
    # ...but it must not veto an install the retry completed.
    assert result["steps"][3]["ok"] is True
    assert ["/n/pw", "install-browser", "chromium", "--with-deps"] in calls
    assert ["/n/pw", "install-browser", "chromium"] in calls


def test_a_failed_retry_after_a_refused_with_deps_carries_the_engine_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When both attempts fail, the last step decides the job, so it is the one
    that must name the engine's remedy."""
    monkeypatch.setattr(mod.os_deps, "with_deps_supported", lambda: True)
    monkeypatch.setattr(
        mod.os_deps, "missing_deps_hint", lambda engine="chromium": f"install-deps {engine}"
    )
    _wire(monkeypatch, {"npm": "/n/npm", "playwright-cli": "/n/pw"}, {"/n/pw": (1, "", "boom")})

    steps = mod.install_browser("firefox")["steps"]

    assert [s["name"] for s in steps] == [
        "install-browser-firefox",
        "install-browser-firefox-no-deps",
    ]
    assert steps[0]["hint"] == ""
    assert steps[1]["hint"] == "install-deps firefox"


def test_the_browser_step_hint_is_asked_for_the_engine_it_downloads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Chromium package list does not make Firefox launch, so the remedy is
    composed per engine."""
    asked: list[str] = []

    def hint(engine: str = "chromium") -> str:
        asked.append(engine)
        return f"hint for {engine}"

    monkeypatch.setattr(mod.os_deps, "missing_deps_hint", hint)
    _wire(monkeypatch, {"npm": "/n/npm", "playwright-cli": "/n/pw"})
    mod.install()
    mod.install_browser("webkit")
    assert asked == ["chromium", "webkit"]


def test_a_zero_exit_carrying_the_host_validation_warning_is_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MEASURED: ``install-browser`` exits 0 when the host is missing libraries.

    Playwright classifies it as a warning, so trusting the exit code reports a
    browser that cannot launch as installed -- the panel goes green and the real
    error arrives at the user's first browse as an opaque stack trace. The step
    must fail, and must carry the remedy.
    """
    monkeypatch.setattr(
        mod.os_deps, "missing_deps_hint", lambda engine="chromium": "sudo dnf install -y nss"
    )
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
    browser_step = result["steps"][2]
    assert browser_step["name"] == "install-browser"
    assert browser_step["ok"] is False
    # rc stays 0 -- the exit code is honestly reported, it is just not the verdict.
    assert browser_step["returncode"] == 0
    assert "missing dependencies" in browser_step["stderr"]
    assert "sudo dnf install -y nss" in browser_step["stderr"]
    # The skills step never runs behind a browser that cannot launch.
    assert [s["name"] for s in result["steps"]] == [
        "npm-install-global",
        "stage-node",
        "install-browser",
    ]


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
    assert result["steps"][2]["ok"] is False
    assert "missing dependencies" in result["steps"][2]["stderr"]


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
        "stage-node",
        "install-browser",
        "install-skills",
    ]


def test_a_failed_browser_step_carries_the_manual_remedy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On a host whose libraries only root can install, the failure detail must
    carry the command that resolves it -- the settings panel shows that detail
    verbatim, so this is the whole remediation surface."""
    monkeypatch.setattr(
        mod.os_deps, "missing_deps_hint", lambda engine="chromium": "sudo dnf install -y nss"
    )
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


def test_the_remedy_and_stderr_tail_survive_output_long_enough_to_hit_the_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The library list and remedy stay visible after verbose download output."""
    hint = "sudo dnf install -y nss"
    monkeypatch.setattr(mod.os_deps, "missing_deps_hint", lambda engine="chromium": hint)
    stderr = "x" * 50_000 + "\nMissing libraries:\n    libgtk-4.so.1"
    _wire(
        monkeypatch,
        {"npm": "/n/npm", "playwright-cli": "/n/pw"},
        {"/n/pw": (1, "", stderr)},
    )

    result = mod.install()

    step = result["steps"][-1]
    assert step["hint"] == hint
    assert "Missing libraries:\n    libgtk-4.so.1" in step["stderr"]
    assert step["stderr"].endswith(hint)


def test_a_successful_step_carries_no_remedy(monkeypatch: pytest.MonkeyPatch) -> None:
    """The hint is failure-only: on a green install it would read as a warning."""
    monkeypatch.setattr(
        mod.os_deps, "missing_deps_hint", lambda engine="chromium": "sudo dnf install -y nss"
    )
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
        "stage-node",
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
    assert [s["name"] for s in result["steps"]] == [
        "npm-install-global",
        "stage-node",
        "install-browser",
    ]
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


def test_cli_env_trusts_the_os_certificate_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """Node children verify TLS against the OS store as well as Node's bundled roots.

    A GUI-launched gateway has no shell profile, so a corporate proxy CA that the
    OS already trusts would otherwise never reach the browser-download child.
    """
    monkeypatch.delenv("NODE_USE_SYSTEM_CA", raising=False)

    assert mod.cli_env()["NODE_USE_SYSTEM_CA"] == "1"


def test_cli_env_keeps_a_user_set_system_ca_choice(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NODE_USE_SYSTEM_CA", "0")

    assert mod.cli_env()["NODE_USE_SYSTEM_CA"] == "0"


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


@pytest.mark.skipif(os.name == "nt", reason="POSIX managed npm bin layout")
def test_agent_path_leads_with_the_sealed_managed_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    crew = tmp_path / "crew"
    home = tmp_path / "home"
    monkeypatch.setattr(env_mod, "peek_data_home", lambda: crew)
    monkeypatch.setattr(mod, "config_dir", lambda: crew)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("MISE_DATA_DIR", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)

    entries = env_mod.augmented_path("/usr/bin").split(os.pathsep)

    assert entries[0] == str(crew / "playwright-cli" / "managed-bin")
    assert Path(entries[0]).parent == mod._managed_cli_root()
    assert entries.index(str(home / ".local" / "bin")) > 0


class TestPerEngineDownloads:
    """Each engine is its own download, and the engine name never reaches argv raw."""

    def test_engines_are_reported_individually(self, monkeypatch, tmp_path):
        cache = tmp_path / "ms-playwright"
        cache.mkdir(exist_ok=True)
        _complete(cache / "chromium-1208")
        _complete(cache / "webkit-2248")
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
        monkeypatch.setattr(mod, "cli_command", lambda cli=None: [cli or str(fake_cli)])
        seen: list[list[str]] = []

        def _fake_step(
            name, argv, timeout, hint="", failure_signal=None, hint_requires_signal=False
        ):
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

    def test_install_browser_reports_its_stages_around_the_download(self, monkeypatch, tmp_path):
        fake_cli = tmp_path / "playwright-cli"
        fake_cli.write_text("")
        monkeypatch.setattr(mod, "cli_path", lambda: str(fake_cli))
        monkeypatch.setattr(mod, "cli_command", lambda cli=None: [cli or str(fake_cli)])
        events: list[str] = []

        def fake_run(argv, timeout):
            events.append("spawn:" + argv[-1])
            return (0, "", "")

        monkeypatch.setattr(mod, "_run", fake_run)

        result = mod.install_browser("firefox", on_stage=events.append)

        assert result["ok"] is True
        assert events == ["downloading_browser", "spawn:firefox", "finishing"]

    def test_the_engine_still_reaches_argv_once_on_a_host_without_the_flag(
        self, monkeypatch, tmp_path
    ):
        """The no-flag path must keep the engine argument: dropping it would
        silently download Chromium while reporting the engine the user asked for."""
        fake_cli = tmp_path / "playwright-cli"
        fake_cli.write_text("")
        monkeypatch.setattr(mod, "cli_path", lambda: str(fake_cli))
        monkeypatch.setattr(mod, "cli_command", lambda cli=None: [cli or str(fake_cli)])
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

    @pytest.mark.parametrize(
        ("rc", "reason"),
        [
            (mod.INTERRUPTED_RC, mod._INTERRUPTED_REASON),
            (mod.TIMEOUT_RC, "timed out after 1s: pw install-browser chromium"),
        ],
        ids=["interrupted", "timed-out"],
    )
    def test_an_interrupted_or_timed_out_step_carries_no_missing_deps_hint(
        self, monkeypatch, rc, reason
    ):
        """A stopped or expired download did not fail for want of OS libraries,
        so the remediation line must not be attached to it."""
        hint = "sudo dnf install -y nss"
        monkeypatch.setattr(mod, "_run", lambda argv, timeout: (rc, "", reason))

        step = mod._step("install-browser", ["pw", "install-browser", "chromium"], 1.0, hint=hint)

        assert step["ok"] is False
        assert step["returncode"] == rc
        assert step["hint"] == ""
        assert hint not in step["stderr"]
        assert step["stderr"] == reason

    def test_an_ordinary_failure_still_ends_with_the_hint(self, monkeypatch):
        hint = "sudo dnf install -y nss"
        monkeypatch.setattr(mod, "_run", lambda argv, timeout: (1, "", "Missing libraries"))

        step = mod._step("install-browser", ["pw", "install-browser", "chromium"], 1.0, hint=hint)

        assert step["hint"] == hint
        assert step["stderr"] == f"Missing libraries\n\n{hint}"

    def test_a_hint_that_requires_the_signal_is_withheld_from_an_unrelated_failure(
        self, monkeypatch
    ):
        """A verdict about the host is evidenced by the missing-library report
        alone; a download that failed for another reason keeps its own error."""
        hint = "WebKit is not supported on this operating system"
        monkeypatch.setattr(mod, "_run", lambda argv, timeout: (1, "", "server returned code 503"))

        step = mod._step(
            "install-browser",
            ["pw", "install-browser", "webkit"],
            1.0,
            hint=hint,
            failure_signal=mod.os_deps.host_deps_unsatisfied,
            hint_requires_signal=True,
        )

        assert step["ok"] is False
        assert step["hint"] == ""
        assert step["stderr"] == "server returned code 503"

    @pytest.mark.parametrize("rc", [0, 1], ids=["exit-0", "exit-1"])
    def test_a_hint_that_requires_the_signal_rides_on_a_signalled_failure(self, monkeypatch, rc):
        hint = "WebKit is not supported on this operating system"
        stderr = "Host system is missing dependencies to run browsers.\n    libgtk-4.so.1"
        monkeypatch.setattr(mod, "_run", lambda argv, timeout: (rc, "", stderr))

        step = mod._step(
            "install-browser",
            ["pw", "install-browser", "webkit"],
            1.0,
            hint=hint,
            failure_signal=mod.os_deps.host_deps_unsatisfied,
            hint_requires_signal=True,
        )

        assert step["ok"] is False
        assert step["hint"] == hint
        assert step["stderr"] == f"{stderr}\n\n{hint}"

    def test_credential_straddling_truncation_boundary_is_still_redacted(self, monkeypatch, caplog):
        """A URL credential whose ``@`` anchor sits past the display cap.

        Truncating first would split ``://user:pass@host`` so the trailing
        ``@`` is gone; the regex does not match, leaking the password
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
        # and confirm it does not survive. The inline-credential URL shape is
        # now caught by the SHARED pass (whose marker is "[REDACTED:
        # credential]") before the npm-specific patterns run, so accept either
        # redaction marker -- what matters is that the secret is gone.
        assert "[REDACTED" in step["stderr"]
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
        """Redaction must not blow up super-linearly on adversarial input.

        **What this asserts, and why it is not a tight ratio.** The bound
        this test exists to defend is the gap between LINEAR and CATASTROPHIC,
        which is the gap between milliseconds and seconds-to-minutes. It does
        not need to resolve 2.0x from 3.0x, and trying to do so is what made it
        flake three separate times:

        * originally, one ``perf_counter`` sample per size — billed for
          whatever the OS gave the sibling xdist workers;
        * then ``thread_time`` + best-of-3, which removed the cross-worker
          noise but not the tick quantization on Windows (~15.625ms steps, so
          6 ticks over 2 ticks reports exactly 3.0 and fails ``< 3.0``);
        * then an adaptive repeat count to clear the tick, which still failed
          CI at **3.07x** on the 3.12 shard — the ONE shard that runs under
          ``--cov``, whose tracer bills every ``re`` call unevenly across the
          two samples while the 3.10 shard (``--no-cov``) passed.

        Measured directly: for genuinely linear code, twelve independent
        best-of-3 ratio measurements on one machine spread **1.53x to 2.50x**.
        A ±0.5 band around 2.0 leaves no room under a 3.0 ceiling, so the
        ratio is measuring scheduler and tracer noise, not scaling.

        So the shape is asserted where the code HAS structure to observe, and
        the timing bound is made generous enough that only catastrophe trips
        it — the rule ``TestIsDeniedReDoSResistance`` and
        ``TestUserRegexReDoSGate`` already follow ("only has to separate
        linear from catastrophic … not assert a sub-100ms wall clock on a
        shared, parallel CI runner"):

        * **Structural half, deterministic:** the pattern is checked to carry
          a bounded quantifier. Catastrophic backtracking on this input needs
          an unbounded one, so its ABSENCE is the actual guarantee — and this
          half cannot flake at all.
        * **Timing half, generously bounded:** doubling the input must not
          cost more than :data:`_CATASTROPHIC_CEILING`. Real quadratic growth
          on 50k chars runs for seconds; the measured linear cost is ~30ms.
          Two orders of magnitude of headroom is what makes it stable.
        """
        import re
        import time

        #: Separates linear from catastrophic with room for a loaded runner and
        #: a coverage tracer. The linear cost of 50k chars is ~30ms here; a
        #: genuinely quadratic matcher on the same input takes seconds. Anything
        #: in between is noise, and this test declines to adjudicate it.
        _CATASTROPHIC_CEILING = 2.0

        # Adversarial: all chars match the env-var prefix class [A-Z0-9_], the
        # shape that triggered the original catastrophic backtracking before
        # the {0,40} bound was added.
        small = "A" * 25_000
        large = "A" * 50_000

        # ── The structural half: the bound that PREVENTS the blow-up ──
        # The keyword pattern is the one that backtracked catastrophically, and
        # ``{0,40}`` is precisely what fixed it: an unbounded run before a
        # required keyword is what explodes on a long class-matching input
        # (measured as a 120s timeout on 50 KB of stderr — see
        # ``_NPM_SECRET_RES``). Asserting the bound is PRESENT is both stronger
        # and completely deterministic, where timing it is neither.
        keyword_res = [
            pattern.pattern for pattern in mod._NPM_SECRET_RES if "TOKEN" in pattern.pattern
        ]
        assert keyword_res, "the keyword redaction pattern is gone"
        for pattern in keyword_res:
            assert re.search(r"\{\d*,\d+\}", pattern), (
                "the keyword prefix lost its bounded quantifier, which is the only "
                f"thing keeping it linear on a long [A-Z0-9_] run: {pattern}"
            )

        # ── The timing half: catastrophe only ──
        mod.redact_install_output(small)  # warm up (JIT, import overhead)
        start = time.thread_time()
        mod.redact_install_output(large)
        elapsed = time.thread_time() - start
        assert elapsed < _CATASTROPHIC_CEILING, (
            f"redacting {len(large)} adversarial chars took {elapsed:.3f}s, over the "
            f"{_CATASTROPHIC_CEILING}s catastrophic-backtracking ceiling — the "
            "matcher is no longer linear in input length"
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


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable and permission semantics")
class TestCliPathTrust:
    @staticmethod
    def _executable(path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        path.chmod(0o755)
        return path

    @staticmethod
    def _isolate(
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> tuple[Path, Path]:
        home = tmp_path / "home"
        crew = home / ".kiro" / "crew"
        crew.mkdir(parents=True)
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("PATH", "")
        monkeypatch.setattr(mod, "config_dir", lambda: crew)
        monkeypatch.setattr(mod, "_system_cli_candidates", lambda: (), raising=False)
        monkeypatch.setattr(mod, "_agent_writable_roots", lambda: (), raising=False)
        monkeypatch.setattr(mod, "_warned_cli_refusals", set(), raising=False)
        return home, crew

    def test_the_vetted_top_level_managed_leaf_is_resolved(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _home, crew = self._isolate(tmp_path, monkeypatch)
        cli = self._executable(crew / "playwright-cli" / "bin" / mod.CLI_BIN)

        assert mod.cli_path() == str(cli.resolve())

    def test_install_refuses_up_front_when_the_workspace_contains_the_data_home(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        home, crew = self._isolate(tmp_path, monkeypatch)
        monkeypatch.setattr(mod, "_agent_writable_roots", lambda: (home.resolve(),))
        calls = _wire(monkeypatch, {"npm": "/n/npm"})

        result = mod.install()

        assert result["ok"] is False
        assert [s["name"] for s in result["steps"]] == ["check-install-boundary"]
        detail = result["steps"][0]["stderr"]
        assert f"{home.resolve()} contains the Kiro Crew data home ({crew})" in detail
        assert "agent-writable" in detail
        assert calls == []

    def test_install_proceeds_over_the_data_home_when_a_vetted_system_launcher_resolves(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        home, _crew = self._isolate(tmp_path, monkeypatch)
        system = self._executable(tmp_path / "opt" / "bin" / mod.CLI_BIN)
        monkeypatch.setattr(mod, "_agent_writable_roots", lambda: (home.resolve(),))
        monkeypatch.setattr(mod, "_system_cli_candidates", lambda: (system,))
        monkeypatch.setattr(mod, "_gateway_writable_component", lambda candidate, resolved: None)
        _wire(monkeypatch, {"npm": "/n/npm"})

        result = mod.install()

        assert result["steps"][0]["name"] == "npm-install-global"

    def test_install_proceeds_when_the_workspace_is_beside_the_data_home(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._isolate(tmp_path, monkeypatch)
        workspace = tmp_path / "workplace" / "kirocrew-workspace"
        workspace.mkdir(parents=True)
        monkeypatch.setattr(mod, "_agent_writable_roots", lambda: (workspace.resolve(),))
        _wire(monkeypatch, {"npm": "/n/npm"})

        result = mod.install()

        assert result["steps"][0]["name"] == "npm-install-global"

    def test_a_refused_launcher_found_after_install_names_it_and_the_reason(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _home, crew = self._isolate(tmp_path, monkeypatch)
        leaf = crew / "playwright-cli"
        cli = self._executable(leaf / "bin" / mod.CLI_BIN)
        monkeypatch.setattr(mod, "_agent_writable_roots", lambda: (leaf.resolve(),))
        _wire(monkeypatch, {"npm": "/n/npm"})

        result = mod.install()

        step = result["steps"][-1]
        assert step["name"] == "resolve-binary"
        assert step["stderr"] == (
            f"{mod.CLI_BIN} was installed at {cli} but refused: "
            f"the resolved executable is inside the agent-writable tree {leaf.resolve()}"
        )

    def test_install_reports_an_absent_managed_launcher_as_not_found(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._isolate(tmp_path, monkeypatch)
        _wire(monkeypatch, {"npm": "/n/npm"})

        result = mod.install()

        step = result["steps"][-1]
        assert step["name"] == "resolve-binary"
        assert step["stderr"] == (
            f"{mod.CLI_BIN} was not found in the managed tools leaf after a successful install"
        )

    def test_a_user_local_refusal_names_the_boundary(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        home, _crew = self._isolate(tmp_path, monkeypatch)
        crew = home / ".local" / "share" / "crew"
        crew.mkdir(parents=True)
        monkeypatch.setattr(mod, "config_dir", lambda: crew)
        self._executable(crew / "playwright-cli" / "bin" / mod.CLI_BIN)

        reason = mod._managed_leaf_unresolved_reason()

        assert "under the user-local tree" in reason

    def test_a_path_first_shim_is_not_resolved(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        self._isolate(tmp_path, monkeypatch)
        shim = self._executable(tmp_path / "path-bin" / mod.CLI_BIN)
        monkeypatch.setenv("PATH", str(shim.parent))

        with caplog.at_level("WARNING", logger=mod.__name__):
            assert mod.cli_path() is None

        warnings = [record.getMessage() for record in caplog.records if record.levelno >= 30]
        assert len(warnings) == 1
        assert repr(str(shim.resolve())) in warnings[0]
        assert "PATH" in warnings[0]

    def test_a_legacy_user_local_shim_is_ignored(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        home, _crew = self._isolate(tmp_path, monkeypatch)
        shim = self._executable(home / ".local" / "bin" / mod.CLI_BIN)
        monkeypatch.setenv("PATH", str(shim.parent))

        with caplog.at_level("WARNING", logger=mod.__name__):
            assert mod.cli_path() is None

        warning = next(record.getMessage() for record in caplog.records if record.levelno >= 30)
        assert repr(str(shim.resolve())) in warning
        assert ".local" in warning

    def test_a_project_shim_is_not_resolved(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        self._isolate(tmp_path, monkeypatch)
        project = tmp_path / "project"
        shim = self._executable(project / "bin" / mod.CLI_BIN)
        monkeypatch.setenv("PATH", str(shim.parent))
        monkeypatch.setattr(mod, "_agent_writable_roots", lambda: (project.resolve(),))

        with caplog.at_level("WARNING", logger=mod.__name__):
            assert mod.cli_path() is None

        warning = next(record.getMessage() for record in caplog.records if record.levelno >= 30)
        assert repr(str(shim.resolve())) in warning
        assert "agent-writable" in warning

    def test_a_gateway_user_writable_system_candidate_is_refused(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        self._isolate(tmp_path, monkeypatch)
        candidate = self._executable(tmp_path / "system-bin" / mod.CLI_BIN)
        monkeypatch.setattr(mod, "_system_cli_candidates", lambda: (candidate,))

        with caplog.at_level("WARNING", logger=mod.__name__):
            assert mod.cli_path() is None

        warning = next(record.getMessage() for record in caplog.records if record.levelno >= 30)
        assert repr(str(candidate.resolve())) in warning
        assert "writable by the gateway user" in warning

    def test_legacy_user_local_launcher_is_reported_refused(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """User-local upgrade journey: a ``~/.local/bin`` launcher is present but
        declined, so the surface can explain the one-time managed reinstall."""
        home, _crew = self._isolate(tmp_path, monkeypatch)
        shim = self._executable(home / ".local" / "bin" / mod.CLI_BIN)
        monkeypatch.setenv("PATH", str(shim.parent))

        assert mod.cli_path() is None
        assert mod.legacy_launcher_refused() is True

    def test_version_manager_launcher_is_reported_refused(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Version-manager upgrade journey: a PATH-resolved launcher (as a mise,
        nvm, or Volta shim dir would contribute) is present but declined."""
        self._isolate(tmp_path, monkeypatch)
        shim = self._executable(tmp_path / "vm-shims" / mod.CLI_BIN)
        monkeypatch.setenv("PATH", str(shim.parent))

        assert mod.cli_path() is None
        assert mod.legacy_launcher_refused() is True

    def test_no_launcher_at_all_is_not_reported_refused(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Nothing on disk or PATH: not a refusal, so the plain not-installed
        message stays, not the reinstall explanation."""
        self._isolate(tmp_path, monkeypatch)

        assert mod.cli_path() is None
        assert mod.legacy_launcher_refused() is False

    def test_a_trusted_managed_launcher_is_not_reported_refused(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A resolvable managed launcher is not a refusal even if a legacy shim
        also sits on PATH -- the trusted copy wins and the panel is available."""
        home, crew = self._isolate(tmp_path, monkeypatch)
        self._executable(crew / "playwright-cli" / "bin" / mod.CLI_BIN)
        shim = self._executable(home / ".local" / "bin" / mod.CLI_BIN)
        monkeypatch.setenv("PATH", str(shim.parent))

        assert mod.cli_path() is not None
        assert mod.legacy_launcher_refused() is False

    def test_a_trusted_system_launcher_resolves_and_is_not_a_refusal(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When no managed leaf exists, a trusted fixed system candidate resolves
        -- the panel is available, so this is not a refusal."""
        self._isolate(tmp_path, monkeypatch)
        candidate = self._executable(tmp_path / "system-bin" / mod.CLI_BIN)
        monkeypatch.setattr(mod, "_system_cli_candidates", lambda: (candidate,))
        # The fixed system location is trusted here; only the agent-writable and
        # user-local floors reject, and this candidate is under neither.
        monkeypatch.setattr(mod, "_gateway_writable_component", lambda c, r: None)

        assert mod.cli_path() == str(candidate.resolve())
        assert mod.legacy_launcher_refused() is False

    def test_an_agent_writable_path_shim_is_not_reported_as_legacy(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A planted shim on PATH that resolves inside an agent-writable tree is
        refused, but it is NOT a user's legacy install -- a reinstall would not
        address it -- so the plain refusal message stays, not the reinstall copy."""
        self._isolate(tmp_path, monkeypatch)
        project = tmp_path / "project"
        shim = self._executable(project / "bin" / mod.CLI_BIN)
        monkeypatch.setenv("PATH", str(shim.parent))
        monkeypatch.setattr(mod, "_agent_writable_roots", lambda: (project.resolve(),))

        assert mod.cli_path() is None
        assert mod.legacy_launcher_refused() is False

    def test_a_user_owned_system_install_refused_as_writable_is_legacy(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A user's own machine install -- a Homebrew ``/opt/homebrew/bin`` or an
        npm-global ``/usr/local/bin`` launcher -- is a fixed system candidate
        refused as writable by the gateway user. It IS the user's own install, so
        a one-time managed reinstall is the remedy and the surface explains it."""
        self._isolate(tmp_path, monkeypatch)
        candidate = self._executable(tmp_path / "homebrew" / "bin" / mod.CLI_BIN)
        monkeypatch.setattr(mod, "_system_cli_candidates", lambda: (candidate,))

        assert mod.cli_path() is None
        assert mod.legacy_launcher_refused() is True

    def test_agent_writable_roots_unverifiable_is_not_reported_as_legacy(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When the agent-writable roots cannot be verified the candidate is
        refused as an anomaly, not a user install -- a reinstall would not
        address it -- so the plain refusal message stays, not the reinstall copy.
        This case does not carry the ``agent-writable tree`` substring, which the
        earlier reason-string classification would have mis-read as legacy."""
        self._isolate(tmp_path, monkeypatch)
        shim = self._executable(tmp_path / "vm-shims" / mod.CLI_BIN)
        monkeypatch.setenv("PATH", str(shim.parent))

        def _boom() -> tuple[Path, ...]:
            raise RuntimeError("roots backend unavailable")

        monkeypatch.setattr(mod, "_agent_writable_roots", _boom)

        assert mod.cli_path() is None
        assert mod.legacy_launcher_refused() is False


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink and permission semantics")
class TestGatewayWritableComponentWalksEveryDirectory:
    """The writability question is asked of every directory the walk reads.

    A lexical chain over the collapsed path never names a symlink hop in the
    middle of a chain, nor the directory holding a symlinked directory component,
    and either one's owner chooses what the candidate resolves to. These fixtures
    present every component as unwritable by this process except ONE, so nothing
    but the enumeration can decide the verdict.
    """

    @staticmethod
    def _executable(path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        path.chmod(0o755)
        return path

    @staticmethod
    def _writable_only(monkeypatch: pytest.MonkeyPatch, loose: Path | None) -> None:
        """Every component reads as unwritable by this process, except *loose*."""
        loose_real = os.path.realpath(loose) if loose is not None else None
        real_stat = os.stat

        def fake_stat(path, *args, **kwargs):
            info = real_stat(path, *args, **kwargs)
            if os.path.realpath(str(path)) == loose_real:
                mode = info.st_mode | 0o002
            else:
                mode = info.st_mode & ~0o022
            return os.stat_result((mode, *tuple(info)[1:]))

        def fake_access(path, mode, **kwargs):
            if mode == os.X_OK:
                return True
            return os.path.realpath(str(path)) == loose_real

        monkeypatch.setattr(os, "stat", fake_stat)
        monkeypatch.setattr(os, "access", fake_access)
        monkeypatch.setattr(mod, "_agent_writable_roots", lambda: ())

    def _hop_chain(self, tmp_path: Path) -> tuple[Path, Path, Path]:
        """``trusted/cli -> writable/hop -> trusted/real``; returns entry, writable, target."""
        trusted = tmp_path / "trusted"
        writable = tmp_path / "writable"
        writable.mkdir()
        target = self._executable(trusted / "real")
        middle = writable / "hop"
        middle.symlink_to(target)
        entry = trusted / mod.CLI_BIN
        entry.symlink_to(middle)
        return entry, writable, target

    def _symlinked_component_chain(self, tmp_path: Path) -> tuple[Path, Path, Path]:
        """``prefix/bin -> holder/bin``; returns entry, prefix, leaf."""
        prefix = tmp_path / "prefix"
        prefix.mkdir()
        leaf = self._executable(tmp_path / "holder" / "bin" / mod.CLI_BIN)
        (prefix / "bin").symlink_to(leaf.parent)
        return prefix / "bin" / mod.CLI_BIN, prefix, leaf

    def test_a_writable_hop_in_the_middle_of_a_chain_refuses_the_candidate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        entry, writable, _target = self._hop_chain(tmp_path)
        self._writable_only(monkeypatch, writable)

        resolved, reason, _kind = mod._system_candidate(entry)

        assert resolved is None
        assert reason is not None
        assert "writable by the gateway user" in reason
        assert str(writable.resolve()) in reason, "must name the offending directory"

    def test_a_writable_parent_of_a_symlinked_component_refuses_the_candidate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``prefix`` holds the ``bin`` link; whoever writes it re-points the whole tree.

        The collapsed path's chain runs ``holder/bin``, ``holder``, ... and never
        names ``prefix``.
        """
        entry, prefix, _leaf = self._symlinked_component_chain(tmp_path)
        self._writable_only(monkeypatch, prefix)

        resolved, reason, _kind = mod._system_candidate(entry)

        assert resolved is None
        assert reason is not None
        assert "writable by the gateway user" in reason
        assert str(prefix.resolve()) in reason, "must name the offending directory"

    def test_a_chain_through_unwritable_directories_is_still_accepted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Strictly a widening: the same hop shape with nothing writable is accepted."""
        entry, _writable, target = self._hop_chain(tmp_path)
        self._writable_only(monkeypatch, None)

        assert mod._system_candidate(entry) == (target.resolve(), None, None)

    def test_the_direct_launcher_file_walk_uses_the_same_enumeration(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The non-executable package-file check asks the same question over the same walk."""
        entry, writable, _target = self._hop_chain(tmp_path)
        self._writable_only(monkeypatch, writable)

        resolved, reason = mod._resolve_executable_file_for_system(entry)

        assert resolved is None
        assert reason is not None
        assert str(writable.resolve()) in reason

    def test_a_walk_that_cannot_be_enumerated_names_the_whole_candidate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Unknown is not shown-to-be-unwritable: ``None`` from the walk refuses."""
        leaf = self._executable(tmp_path / "bin" / mod.CLI_BIN)
        self._writable_only(monkeypatch, None)
        monkeypatch.setattr(mod.platform_compat, "traversed_components", lambda _path: None)

        assert mod._gateway_writable_component(leaf, leaf.resolve()) == leaf

    def test_windows_keeps_the_lexical_chain_over_the_resolved_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The walker is POSIX-shaped; Windows asks ``os.access`` over ``resolved.parents``."""
        leaf = self._executable(tmp_path / "bin" / mod.CLI_BIN)
        self._writable_only(monkeypatch, leaf.parent)
        monkeypatch.setattr(mod.platform_compat, "IS_WINDOWS", True)

        def never(_path):
            raise AssertionError("the POSIX walker must not run on Windows")

        monkeypatch.setattr(mod.platform_compat, "traversed_components", never)

        assert mod._gateway_writable_component(leaf, leaf.resolve()) == leaf.parent.resolve()
