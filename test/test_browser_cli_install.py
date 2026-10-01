"""Detection, install sequencing, and the capability-availability probe."""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
from typing import Any

import pytest

from kiro_crew import env as env_mod
from kiro_crew.browser_cli import install as mod

# The real implementation, captured before the autouse fixture below replaces
# ``mod._required_revisions`` with a degradation stub. Revision-aware tests
# restore THIS (re-reading ``mod._required_revisions`` there would just re-bind
# the stub to itself, silently leaving every test on the fallback path).
_REAL_REQUIRED_REVISIONS = mod._required_revisions
# The real cache resolver, captured for the same reason: the autouse fixture
# below points ``_browsers_cache_dir`` at a scratch directory.
_REAL_BROWSERS_CACHE_DIR = mod._browsers_cache_dir


@pytest.fixture(autouse=True)
def _clear_node_bin_dir_caches() -> None:
    """Clear the Node-tool search caches used by ``_node_version`` and npm install.

    ``cli_path()`` uses a separate absolute managed/system resolver. Node and npm
    still resolve through ``find_node_tool`` and must not inherit a real-machine version-manager path
    from an earlier test in the worker.
    """
    env_mod.node_bin_dirs.cache_clear()
    env_mod._node_all_bin_dirs.cache_clear()
    yield
    env_mod.node_bin_dirs.cache_clear()
    env_mod._node_all_bin_dirs.cache_clear()


@pytest.fixture(autouse=True)
def isolated_browser_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep ``browser_ok`` off the developer's real Playwright cache.

    Also defaults ``_required_revisions`` to ``None`` so a test that does not
    opt into revision-awareness exercises the documented degradation path
    (presence-only prefix match) deterministically, independent of any vetted
    CLI install on the host. Tests that assert revision-exact behaviour override
    this explicitly.
    """
    cache = tmp_path / "ms-playwright"
    cache.mkdir()
    monkeypatch.setattr(mod, "_browsers_cache_dir", lambda: cache)
    monkeypatch.setattr(mod, "_required_revisions", lambda: None)
    return cache


@pytest.fixture(autouse=True)
def _default_no_os_deps(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default every test to a host with no OS-library remedy.

    The browser step asks :mod:`kiro_crew.browser_cli.os_deps` for its remedy,
    and the answer is read from the DEVELOPER's ``/etc/os-release`` otherwise --
    which would make the detail assertions here pass on macOS and fail on
    Ubuntu. Tests that care about the hint or the flag opt in explicitly.
    """
    monkeypatch.setattr(mod.os_deps, "with_deps_supported", lambda: False)
    monkeypatch.setattr(mod.os_deps, "missing_deps_hint", lambda engine="chromium": "")


def _complete(directory: Path) -> Path:
    """Create a browser build directory the way a finished download leaves it."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / mod._INSTALLATION_MARKER).write_text("")
    return directory


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

    monkeypatch.setattr(
        mod,
        "find_node_tool",
        lambda name, base_path=None: tools.get(name)
        or ("/n/node" if name == "node" and tools.get("npm") else None),
    )
    monkeypatch.setattr(mod, "cli_path", lambda: tools.get(mod.CLI_BIN))
    monkeypatch.setattr(
        mod,
        "_pinned_managed_cli_root",
        lambda: contextlib.nullcontext(mod._managed_cli_root()),
    )
    monkeypatch.setattr(
        mod,
        "_node_runtime_executable",
        lambda node: node,
    )
    monkeypatch.setattr(
        mod,
        "_stage_managed_node",
        lambda source: Path("/managed/gateway-node"),
    )
    monkeypatch.setattr(
        mod,
        "cli_command",
        lambda cli=None: (
            [resolved] if (resolved := cli or tools.get(mod.CLI_BIN)) is not None else None
        ),
    )

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

        resolved, reason = mod._system_candidate(entry)

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

        resolved, reason = mod._system_candidate(entry)

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

        assert mod._system_candidate(entry) == (target.resolve(), None)

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


class TestWindowsGatewayCommand:
    @staticmethod
    def _managed_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path]:
        crew = tmp_path / "crew"
        root = crew / "playwright-cli"
        package = root / "node_modules" / "@playwright" / "cli"
        package.mkdir(parents=True)
        cmd = root / "playwright-cli.cmd"
        cmd.write_text("@echo off\n", encoding="utf-8")
        cmd.chmod(0o755)
        node = root / "node.exe"
        node.write_bytes(b"node")
        node.chmod(0o755)
        entry = package / "playwright-cli.js"
        entry.write_text("// cli\n", encoding="utf-8")
        monkeypatch.setattr(mod.platform_compat, "IS_WINDOWS", True)
        monkeypatch.setattr(mod, "config_dir", lambda: crew)
        monkeypatch.setattr(mod, "_system_cli_candidates", lambda: ())
        monkeypatch.setattr(mod, "_agent_writable_roots", lambda: ())
        monkeypatch.setattr(mod, "_warned_cli_refusals", set())
        return root, node, entry

    def test_gateway_invocation_uses_node_and_javascript_not_cmd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _root, node, entry = self._managed_tree(tmp_path, monkeypatch)

        command = mod.cli_command()

        assert command == [str(node.resolve()), str(entry.resolve())]
        assert not any(part.casefold().endswith((".cmd", ".bat")) for part in command)

    def test_javascript_entrypoint_cannot_escape_the_sealed_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _root, _node, entry = self._managed_tree(tmp_path, monkeypatch)
        outside = tmp_path / "outside.js"
        outside.write_text("// attacker\n", encoding="utf-8")
        entry.unlink()
        entry.symlink_to(outside)

        assert mod.cli_command() is None

    def test_install_stages_node_inside_the_managed_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        crew = tmp_path / "crew"
        source = tmp_path / "node.exe"
        source.write_bytes(b"trusted node")
        source.chmod(0o755)
        monkeypatch.setattr(mod.platform_compat, "IS_WINDOWS", True)
        monkeypatch.setattr(mod, "config_dir", lambda: crew)
        probed: list[str] = []
        probe_cwds: list[str | None] = []

        def fake_run(argv, timeout, *, cwd=None):
            probed.append(argv[0])
            probe_cwds.append(cwd)
            return (0, "v24.18.0\n", "")

        monkeypatch.setattr(mod, "_run", fake_run)

        staged = mod._stage_managed_node(str(source))

        assert staged == crew / "playwright-cli" / "node.exe"
        assert staged.read_bytes() == b"trusted node"
        # The smoke run exercised the staged copy inside the leaf, not the source,
        # and ran FROM the leaf rather than from wherever the gateway was started.
        assert probed and Path(probed[-1]).parent == staged.parent
        assert Path(probed[-1]) != source
        assert probe_cwds[-1] == str(staged.parent)

        source.write_bytes(b"replacement node")
        replaced = mod._stage_managed_node(str(source))

        assert replaced == staged
        assert replaced.read_bytes() == b"replacement node"


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
    """The panel's Node-blocked state must not end at "Download Node.js", which is
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
    # Both `os.name` patches live in their OWN throwaway instance, because the
    # `detect()` call below needs the REAL platform back and `monkeypatch.undo()`
    # on the shared instance is not a way to get there: it reverts every record on
    # that instance, including the two module-level autouse fixtures that patched
    # through it. That would send `_browsers_cache_dir` and `_required_revisions`
    # back to production for the rest of the test, and `detect()` would iterdir the
    # developer's real ~/.cache/ms-playwright -- exactly what
    # `isolated_browser_cache` exists to prevent. Exiting the context restores
    # `os.name` alone.
    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(mod.os, "name", "posix")
        posix = mod._standalone_install_command()
        assert "playwright-cli.sh" in posix
        assert "powershell" not in posix
        # Download-then-run rather than a pipe into a shell: a machine locked down
        # enough to need this usually forbids piping the network into `sh`.
        assert "| sh" not in posix
        assert "curl -fsSL" in posix

        patched.setattr(mod.os, "name", "nt")
        windows = mod._standalone_install_command()
        assert "playwright-cli.ps1" in windows
        assert "playwright-cli.sh" not in windows

    # `detect()` is exercised under the REAL platform: with `os.name` patched to
    # "nt", pathlib refuses to build a WindowsPath on Linux and the call dies
    # before the payload exists. The Windows branch above is the helper's job.
    # `_wire` for the same reason every other `detect()` call site here uses it --
    # unwired, `detect()` resolves the real `cli_path()` and spawns the host's own
    # `playwright-cli --version` and `node --version` with the repo root as cwd.
    _wire(monkeypatch, {})
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
        # ``_wire`` pins ``cli_path`` to the launcher AND fakes the version probes, so
        # ``detect()`` below never runs the host's ``node``/``playwright-cli``.
        _wire(monkeypatch, {mod.CLI_BIN: launcher})
        monkeypatch.setattr(
            mod, "_required_revisions", _REAL_REQUIRED_REVISIONS
        )  # use the real one
        _complete(isolated_browser_cache / "chromium-1232")

        assert mod.browsers_present()["chromium"] is True
        assert mod._browser_present() is True
        assert mod.detect()["browser_ok"] is True

    def test_stale_revision_is_not_ready(
        self, monkeypatch: pytest.MonkeyPatch, isolated_browser_cache: Path, tmp_path: Path
    ) -> None:
        """THE regression. chromium-1208 is present, 1232 is required."""
        root = tmp_path / "cli-root"
        launcher = _install_root(root, _MANIFEST)
        _wire(monkeypatch, {mod.CLI_BIN: launcher})
        monkeypatch.setattr(mod, "_required_revisions", _REAL_REQUIRED_REVISIONS)
        _complete(isolated_browser_cache / "chromium-1208")

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
        _complete(isolated_browser_cache / "chromium_headless_shell-1232")

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
        _complete(isolated_browser_cache / "chromium_1232")

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
        _complete(isolated_browser_cache / "firefox-1534")
        _complete(isolated_browser_cache / "webkit-9999")

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
        _complete(isolated_browser_cache / "chromium-1232")

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
        _complete(isolated_browser_cache / "chromium-1208")

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
        _complete(isolated_browser_cache / "chromium-1208")

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
        _complete(isolated_browser_cache / "chromium-1208")  # stale -> not ready
        _complete(isolated_browser_cache / "webkit-2248")  # no required rev -> present

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
        _complete(isolated_browser_cache / "chromium-1232")

        assert mod.browsers_present()["chromium"] is True

    def test_no_cli_means_no_manifest(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(mod, "cli_path", lambda: None)
        monkeypatch.setenv("KIROCREW_PLAYWRIGHT_CLI_HOME", "/nonexistent-prefix")
        assert mod._browsers_manifest_path() is None
        assert _REAL_REQUIRED_REVISIONS() is None


def _attributed_lifecycle_tree(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A launcher, and the registry/bundle of the package attributable to it.

    A real-FILE launcher beside its install prefix's ``node_modules``, which is
    the shape ``_launcher_node_modules`` resolves. Deliberately not a symlink, so
    the seam tests need no symlink privilege and run on every platform.
    """
    prefix = tmp_path / "prefix"
    package = prefix / "node_modules" / "@playwright" / "cli"
    core = package / "node_modules" / "playwright-core"
    (core / "lib" / "tools" / "cli-client").mkdir(parents=True)
    (core / "browsers.json").write_text('{"browsers": []}', encoding="utf-8")
    launcher = prefix / "bin" / "playwright-cli"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\n", encoding="utf-8")
    return (
        launcher,
        core / "lib" / "tools" / "cli-client" / "registry.js",
        core / "lib" / "coreBundle.js",
    )


def _lifecycle_contract_tree(tmp_path: Path) -> tuple[Path, Path]:
    package = tmp_path / "node_modules" / "@playwright" / "cli"
    core = package / "node_modules" / "playwright-core"
    (core / "lib" / "tools" / "cli-client").mkdir(parents=True)
    (core / "browsers.json").write_text('{"browsers": []}', encoding="utf-8")
    registry = core / "lib" / "tools" / "cli-client" / "registry.js"
    bundle = core / "lib" / "coreBundle.js"
    return package, registry, bundle


def test_lifecycle_env_contract_is_confirmed_from_serving_cli_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launcher, registry, bundle = _attributed_lifecycle_tree(tmp_path)
    registry.write_text("process.env.PWTEST_DAEMON_SESSION_DIR", encoding="utf-8")
    bundle.write_text("process.env.PWTEST_SOCKETS_DIR || os.tmpdir()", encoding="utf-8")
    monkeypatch.setattr(mod, "cli_path", lambda: str(launcher))
    mod._source_contains.cache_clear()

    assert mod.cli_lifecycle_env_support() == (mod.SeamSupport.SUPPORTED, "")


def test_lifecycle_env_contract_fails_when_upstream_hook_disappears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launcher, registry, bundle = _attributed_lifecycle_tree(tmp_path)
    registry.write_text("process.env.PWTEST_DAEMON_SESSION_DIR", encoding="utf-8")
    bundle.write_text("// lifecycle socket override removed upstream", encoding="utf-8")
    monkeypatch.setattr(mod, "cli_path", lambda: str(launcher))
    mod._source_contains.cache_clear()

    assert mod.cli_lifecycle_env_support()[0] is mod.SeamSupport.UNSUPPORTED


def test_lifecycle_contract_never_falls_through_to_a_stale_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A launcher's own package answers for it, or nothing does.

    The standalone install prefix is a legitimate fallback for a revision lookup
    but not for a seam verdict: it describes a DIFFERENT install, so letting it
    answer for an unattributable launcher would put the original misreport back
    in a narrower case. Here the standalone package is healthy and the launcher
    is unattributable -- the verdict must be UNVERIFIED, not SUPPORTED.
    """
    _launcher, standalone_registry, standalone_bundle = _attributed_lifecycle_tree(tmp_path)
    standalone_registry.write_text("process.env.PWTEST_DAEMON_SESSION_DIR", encoding="utf-8")
    standalone_bundle.write_text("process.env.PWTEST_SOCKETS_DIR || os.tmpdir()", encoding="utf-8")
    monkeypatch.setenv("KIROCREW_PLAYWRIGHT_CLI_HOME", str(tmp_path / "prefix"))
    orphan = tmp_path / "elsewhere" / "playwright-cli"
    orphan.parent.mkdir(parents=True)
    orphan.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(mod, "cli_path", lambda: str(orphan))
    mod._source_contains.cache_clear()

    # The standalone package on its own would answer SUPPORTED.
    assert mod._cli_package_dirs()
    support, detail = mod.cli_lifecycle_env_support()

    assert support is mod.SeamSupport.UNVERIFIED
    assert str(orphan) in detail


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink launcher shape")
def test_a_symlink_launcher_resolving_into_the_package_is_confirmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shape that DOES attribute by ANCESTRY: an ``npm install -g`` symlink.

    The launcher resolves INTO the package tree, so an ancestor is the package
    and no ``node_modules`` probe is needed. Its sibling above covers the
    real-file shape, which reaches the package the other way.
    """
    package, registry, bundle = _lifecycle_contract_tree(tmp_path)
    registry.write_text("process.env.PWTEST_DAEMON_SESSION_DIR", encoding="utf-8")
    bundle.write_text("process.env.PWTEST_SOCKETS_DIR || os.tmpdir()", encoding="utf-8")
    entrypoint = package / "cli.js"
    entrypoint.write_text("#!/usr/bin/env node\n", encoding="utf-8")
    launcher = tmp_path / "bin" / "playwright-cli"
    launcher.parent.mkdir(parents=True)
    launcher.symlink_to(entrypoint)
    monkeypatch.setenv("KIROCREW_PLAYWRIGHT_CLI_HOME", str(tmp_path / "absent-prefix"))
    monkeypatch.setattr(mod, "cli_path", lambda: str(launcher))
    mod._source_contains.cache_clear()

    assert mod.cli_lifecycle_env_support() == (mod.SeamSupport.SUPPORTED, "")


def test_an_unattributable_launcher_is_unverified_and_names_the_launcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A version-manager shim resolves to itself, not into any package tree.

    No bundle is read, so the CLI's capability is unknown. Reporting that as
    UNSUPPORTED is what sent an operator to upgrade a CLI that carried both
    hooks; the verdict must name the launcher it could not attribute instead.
    """
    shim = tmp_path / "shims" / "playwright-cli"
    shim.parent.mkdir(parents=True)
    shim.write_text('#!/bin/sh\nexec versionctl exec -- "$0" "$@"\n', encoding="utf-8")
    monkeypatch.setenv("KIROCREW_PLAYWRIGHT_CLI_HOME", str(tmp_path / "absent-prefix"))
    monkeypatch.setattr(mod, "cli_path", lambda: str(shim))

    support, detail = mod.cli_lifecycle_env_support()

    assert support is mod.SeamSupport.UNVERIFIED
    assert str(shim) in detail
    assert "attributable" in detail


def test_no_resolved_launcher_is_unverified_rather_than_a_capability_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mod, "cli_path", lambda: None)
    monkeypatch.setenv("KIROCREW_PLAYWRIGHT_CLI_HOME", "/nonexistent-prefix")

    support, detail = mod.cli_lifecycle_env_support()

    assert support is mod.SeamSupport.UNVERIFIED
    assert "no trusted playwright-cli launcher" in detail


def test_a_package_without_a_serving_core_tree_is_unverified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Attribution reached the CLI package but no ``playwright-core`` serves it."""
    prefix = tmp_path / "prefix"
    package = prefix / "node_modules" / "@playwright" / "cli"
    package.mkdir(parents=True)
    launcher = prefix / "bin" / "playwright-cli"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(mod, "cli_path", lambda: str(launcher))

    support, detail = mod.cli_lifecycle_env_support()

    assert support is mod.SeamSupport.UNVERIFIED
    assert str(package) in detail


def test_an_unreadable_bundle_is_unverified_rather_than_unsupported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bundle that cannot be stat'd was never searched for the hook."""
    launcher, registry, _bundle = _attributed_lifecycle_tree(tmp_path)
    registry.write_text("process.env.PWTEST_DAEMON_SESSION_DIR", encoding="utf-8")
    monkeypatch.setattr(mod, "cli_path", lambda: str(launcher))
    mod._source_contains.cache_clear()

    support, detail = mod.cli_lifecycle_env_support()

    assert support is mod.SeamSupport.UNVERIFIED
    assert "could not be read" in detail


@pytest.mark.parametrize(
    ("size", "why"),
    [(0, "an empty bundle is not a bundle without the needle"), (None, "past the size ceiling")],
)
def test_a_bundle_the_reader_refuses_is_unverified_not_unsupported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, size: int | None, why: str
) -> None:
    """The size guard REFUSES to read; a refusal measures nothing.

    Reporting it as UNSUPPORTED is the same defect as an unattributable
    launcher: a claim about bytes that were never searched.
    """
    launcher, registry, bundle = _attributed_lifecycle_tree(tmp_path)
    registry.write_text("process.env.PWTEST_DAEMON_SESSION_DIR", encoding="utf-8")
    bundle.write_text("" if size == 0 else "process.env.PWTEST_SOCKETS_DIR || x", encoding="utf-8")
    if size is None:
        monkeypatch.setattr(mod, "_LIFECYCLE_SOURCE_MAX_BYTES", 1)
    monkeypatch.setattr(mod, "cli_path", lambda: str(launcher))
    mod._source_contains.cache_clear()

    support, detail = mod.cli_lifecycle_env_support()

    assert support is mod.SeamSupport.UNVERIFIED, why
    assert "could not be read" in detail


def test_the_dashboard_layout_gate_separates_attribution_from_capability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sibling gate carries the same three-valued contract."""
    launcher, _registry, bundle = _attributed_lifecycle_tree(tmp_path)
    monkeypatch.setattr(mod, "cli_path", lambda: str(launcher))

    bundle.write_text(
        'makeSocketPath("dashboard", "app"); process.env.PWTEST_SOCKETS_DIR || x',
        encoding="utf-8",
    )
    mod._source_contains.cache_clear()
    assert mod.cli_dashboard_socket_support() == (mod.SeamSupport.SUPPORTED, "")

    bundle.write_text("// dashboard socket layout renamed upstream", encoding="utf-8")
    mod._source_contains.cache_clear()
    assert mod.cli_dashboard_socket_support()[0] is mod.SeamSupport.UNSUPPORTED

    monkeypatch.setattr(mod, "cli_path", lambda: None)
    assert mod.cli_dashboard_socket_support()[0] is mod.SeamSupport.UNVERIFIED


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable and permission semantics")
class TestPosixGatewayCommand:
    @staticmethod
    def _managed_tree(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> tuple[Path, Path, Path, Path]:
        crew = tmp_path / "crew"
        root = crew / "playwright-cli"
        package = root / "lib" / "node_modules" / "@playwright" / "cli"
        package.mkdir(parents=True)
        wrapper = root / "managed-bin" / "playwright-cli"
        wrapper.parent.mkdir(parents=True)
        wrapper.write_text("#!/bin/sh\nexec /agent/node ...\n", encoding="utf-8")
        wrapper.chmod(0o755)
        node = root / "gateway-node"
        node.write_bytes(b"node")
        node.chmod(0o755)
        entry = package / "playwright-cli.js"
        entry.write_text("// cli\n", encoding="utf-8")
        attacker_node = tmp_path / "agent-bin" / "node"
        attacker_node.parent.mkdir(parents=True)
        attacker_node.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
        attacker_node.chmod(0o755)
        monkeypatch.setenv("PATH", str(attacker_node.parent))
        monkeypatch.setattr(mod.platform_compat, "IS_WINDOWS", False)
        monkeypatch.setattr(mod, "config_dir", lambda: crew)
        monkeypatch.setattr(mod, "_system_cli_candidates", lambda: ())
        monkeypatch.setattr(mod, "_agent_writable_roots", lambda: ())
        monkeypatch.setattr(mod, "_warned_cli_refusals", set())
        return wrapper, node, entry, attacker_node

    def test_gateway_invocation_uses_sealed_node_and_javascript_not_the_wrapper(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        wrapper, node, entry, attacker_node = self._managed_tree(tmp_path, monkeypatch)

        command = mod.cli_command()

        assert command == [str(node.resolve()), str(entry.resolve())]
        assert str(wrapper.resolve()) not in command
        assert str(attacker_node.resolve()) not in command

    def test_javascript_entrypoint_cannot_escape_the_sealed_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _wrapper, _node, entry, _attacker_node = self._managed_tree(tmp_path, monkeypatch)
        outside = tmp_path / "outside.js"
        outside.write_text("// attacker\n", encoding="utf-8")
        entry.unlink()
        entry.symlink_to(outside)

        assert mod.cli_command() is None

    def test_install_stages_node_inside_the_managed_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        crew = tmp_path / "crew"
        source = tmp_path / "node"
        source.write_bytes(b"trusted node")
        source.chmod(0o755)
        monkeypatch.setattr(mod.platform_compat, "IS_WINDOWS", False)
        monkeypatch.setattr(mod, "config_dir", lambda: crew)
        monkeypatch.setattr(mod, "_run", lambda argv, timeout, cwd=None: (0, "v24.18.0\n", ""))

        staged = mod._stage_managed_node(str(source))

        assert staged == crew / "playwright-cli" / "gateway-node"
        assert staged.read_bytes() == b"trusted node"
        assert staged.stat().st_mode & 0o777 == 0o500

    def test_install_refuses_a_launcher_script_that_cannot_run_from_the_leaf(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A ``bin/node`` that is a shell script locates the binary it starts
        relative to its own path. It copies cleanly and passes the mode checks,
        but a copy in the managed leaf points at a file that is not there. The
        smoke run is a REAL process here: the copied script runs from the leaf,
        its target is missing, and the install refuses, naming the source. The
        process is the test's own shell script, never a host ``node``, and both
        spawns run in a directory the test owns."""
        crew = tmp_path / "crew"
        bin_dir = tmp_path / "node" / "bin"
        bin_dir.mkdir(parents=True)
        wrapper = bin_dir / "node"
        wrapper.write_text('#!/bin/sh\nexec "$(dirname "$0")/node_real" "$@"\n')
        wrapper.chmod(0o755)
        real = bin_dir / "node_real"
        real.write_text("#!/bin/sh\necho v24.18.0\n")
        real.chmod(0o755)
        monkeypatch.setattr(mod.platform_compat, "IS_WINDOWS", False)
        monkeypatch.setattr(mod, "config_dir", lambda: crew)

        # In place the wrapper works; that is what fooled the mode checks.
        assert mod._run([str(wrapper), "--version"], 20.0, cwd=str(tmp_path))[0] == 0

        with pytest.raises(OSError) as excinfo:
            mod._stage_managed_node(str(wrapper))

        message = str(excinfo.value)
        assert str(wrapper) in message
        assert "does not run from the managed leaf" in message
        # The copied script looked for its sibling next to ITSELF, i.e. inside
        # the leaf. The shell's wording differs (dash: "not found", bash: "No such
        # file or directory"); the path it names is the shell-independent fact.
        assert str(crew / "playwright-cli" / "node_real") in message

        leaf = crew / "playwright-cli"
        assert not (leaf / "gateway-node").exists()
        assert not list(leaf.glob(".gateway-node-*.incoming")), "dead copy left in the leaf"

    def test_install_refuses_a_binary_that_only_runs_under_its_wrapper_environment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Some distributions wrap Node to inject ``LD_LIBRARY_PATH`` for a
        bundled ``libstdc++``. The kernel names the real binary, it is a plain
        executable with no ``#!``, and it still cannot run outside the wrapper.
        Only running the staged copy catches that class, so the smoke run is
        what refuses it; a magic-bytes check would have staged a dead file."""
        crew = tmp_path / "crew"
        binary = tmp_path / "node_real"
        binary.write_bytes(b"\x7fELF needs the wrapper's LD_LIBRARY_PATH")
        binary.chmod(0o755)
        monkeypatch.setattr(mod.platform_compat, "IS_WINDOWS", False)
        monkeypatch.setattr(mod, "config_dir", lambda: crew)
        monkeypatch.setattr(
            mod,
            "_run",
            lambda argv, timeout, cwd=None: (
                127,
                "",
                "error while loading shared libraries: libstdc++.so.6",
            ),
        )

        with pytest.raises(
            OSError, match=r"does not run from the managed leaf \(exit 127: .*libstdc\+\+"
        ):
            mod._stage_managed_node(str(binary))

        assert not (crew / "playwright-cli" / "gateway-node").exists()

    def test_install_refuses_a_copy_that_runs_but_is_not_node(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Exit 0 with no version banner is not a Node either."""
        crew = tmp_path / "crew"
        binary = tmp_path / "node"
        binary.write_bytes(b"\x7fELF something else")
        binary.chmod(0o755)
        monkeypatch.setattr(mod.platform_compat, "IS_WINDOWS", False)
        monkeypatch.setattr(mod, "config_dir", lambda: crew)
        monkeypatch.setattr(
            mod, "_run", lambda argv, timeout, cwd=None: (0, "usage: not-node\n", "")
        )

        with pytest.raises(OSError, match=r"did not report a version"):
            mod._stage_managed_node(str(binary))

        assert not (crew / "playwright-cli" / "gateway-node").exists()


class TestNodeRuntimeExecutableProbe:
    """The staged binary is the one the KERNEL says is running, not the one
    Node names: ``process.execPath`` is argv[0], and a launcher script that
    starts the binary with ``exec -a <script>`` makes Node report the script."""

    def _resolved(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        binary = tmp_path / "bin" / "node_real"
        binary.parent.mkdir(parents=True)
        binary.write_bytes(b"\x7fELF native node")
        binary.chmod(0o755)
        monkeypatch.setattr(mod, "_run", lambda argv, timeout: (0, f"{binary}\n", ""))
        return binary

    def test_probe_asks_the_kernel_first_and_falls_back_to_execpath(self) -> None:
        probe = mod._NODE_EXECUTABLE_PROBE
        assert "realpathSync('/proc/self/exe')" in probe
        assert probe.index("/proc/self/exe") < probe.index("process.execPath")

    def test_probe_output_is_resolved_as_an_executable_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        binary = self._resolved(tmp_path, monkeypatch)
        seen: list[list[str]] = []

        def run(argv: list[str], timeout: float) -> tuple[int, str, str]:
            seen.append(argv)
            return (0, f"{binary}\n", "")

        monkeypatch.setattr(mod, "_run", run)

        assert mod._node_runtime_executable("node") == str(binary.resolve())
        assert seen == [["node", "-p", mod._NODE_EXECUTABLE_PROBE]]

    def test_probe_rejects_a_relative_or_multiline_answer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(mod, "_run", lambda argv, timeout: (0, "node_real\n", ""))
        assert mod._node_runtime_executable("node") is None
        monkeypatch.setattr(mod, "_run", lambda argv, timeout: (0, "/a\n/b\n", ""))
        assert mod._node_runtime_executable("node") is None


class TestSystemGatewayCommand:
    def test_posix_node_candidates_never_consume_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        launcher = tmp_path / "system" / "playwright-cli"
        package = tmp_path / "system" / "lib" / "node_modules" / "@playwright" / "cli"
        package.mkdir(parents=True)
        launcher.parent.mkdir(parents=True, exist_ok=True)
        launcher.write_text("#!/usr/bin/env node\n", encoding="utf-8")
        attacker = tmp_path / "agent-bin" / "node"
        attacker.parent.mkdir()
        attacker.write_text("#!/bin/sh\n", encoding="utf-8")
        monkeypatch.setenv("PATH", str(attacker.parent))
        monkeypatch.setattr(mod.platform_compat, "IS_WINDOWS", False)
        monkeypatch.setattr(mod.platform_compat, "trusted_system_path", lambda: "/usr/bin")
        monkeypatch.setattr(mod.github_runner, "PROVIDER_EXECUTABLE_DIRS", ())

        candidates = mod._system_node_candidates(launcher, package)

        assert Path("/usr/bin/node") in candidates
        assert attacker not in candidates

    def test_a_path_only_node_cannot_complete_a_system_command(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        launcher = tmp_path / "system" / "playwright-cli"
        launcher.parent.mkdir()
        launcher.write_text("#!/usr/bin/env node\n", encoding="utf-8")
        launcher.chmod(0o755)
        package = tmp_path / "system" / "package"
        package.mkdir()
        entry = package / "playwright-cli.js"
        entry.write_text("// cli\n", encoding="utf-8")
        attacker = tmp_path / "agent-bin" / "node"
        attacker.parent.mkdir()
        attacker.write_text("#!/bin/sh\n", encoding="utf-8")
        attacker.chmod(0o755)
        monkeypatch.setenv("PATH", str(attacker.parent))
        monkeypatch.setattr(mod, "_cli_package_for_launcher", lambda _launcher: package)
        monkeypatch.setattr(mod, "_managed_cli_root", lambda: tmp_path / "absent-managed")
        monkeypatch.setattr(mod, "_system_node_candidates", lambda _launcher, _package: ())
        monkeypatch.setattr(
            mod,
            "_resolve_executable_file_for_system",
            lambda _entry: (entry.resolve(), None),
        )

        command, reason = mod._direct_cli_command(str(launcher))

        assert command is None
        assert reason is not None and "no fixed non-writable Node" in reason
        assert str(attacker) not in reason


class TestInstallStages:
    """``install`` reports real stage transitions through ``on_stage``."""

    def test_install_reports_each_stage_before_the_step_it_names(self, monkeypatch):
        events: list[str] = []
        calls = _wire(monkeypatch, {"npm": "/n/npm", "playwright-cli": "/n/pw"})
        original = mod._run

        def recording_run(argv, timeout):
            events.append("spawn:" + ("npm" if argv[0] == "/n/npm" else argv[1]))
            return original(argv, timeout)

        monkeypatch.setattr(mod, "_run", recording_run)

        result = mod.install(on_stage=events.append)

        assert result["ok"] is True
        assert calls
        assert events == [
            "installing_cli",
            "spawn:npm",
            "downloading_browser",
            "spawn:install-browser",
            "installing_skills",
            "spawn:install",
            "finishing",
        ]

    def test_a_failing_stage_callback_does_not_stop_the_install(self, monkeypatch):
        _wire(monkeypatch, {"npm": "/n/npm", "playwright-cli": "/n/pw"})

        def boom(stage: str) -> None:
            raise RuntimeError("loop closed")

        assert mod.install(on_stage=boom)["ok"] is True

    def test_a_failed_download_reports_no_later_stage(self, monkeypatch):
        events: list[str] = []
        _wire(
            monkeypatch,
            {"npm": "/n/npm", "playwright-cli": "/n/pw"},
            {"/n/pw": (1, "", "network unreachable")},
        )

        assert mod.install(on_stage=events.append)["ok"] is False
        assert events == ["installing_cli", "downloading_browser"]


class TestCacheResolutionMatchesPlaywright:
    """``_browsers_cache_dir`` mirrors playwright-core's ``registryDirectory``."""

    @pytest.fixture(autouse=True)
    def _real_resolver(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(mod, "_browsers_cache_dir", _REAL_BROWSERS_CACHE_DIR)
        for name in (
            "PLAYWRIGHT_BROWSERS_PATH",
            "npm_config_playwright_browsers_path",
            "npm_package_config_playwright_browsers_path",
            "XDG_CACHE_HOME",
            "LOCALAPPDATA",
            "INIT_CWD",
            "npm_config_init_cwd",
            "npm_package_config_init_cwd",
        ):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setattr(mod.Path, "home", lambda: tmp_path / "home")

    @staticmethod
    def _platform(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
        for flag in ("IS_LINUX", "IS_MACOS", "IS_WINDOWS"):
            monkeypatch.setattr(mod.platform_compat, flag, flag == name)

    def test_linux_honours_xdg_cache_home(self, monkeypatch, tmp_path):
        self._platform(monkeypatch, "IS_LINUX")
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
        assert mod._browsers_cache_dir() == tmp_path / "xdg" / "ms-playwright"

    def test_linux_without_xdg_uses_the_home_cache(self, monkeypatch, tmp_path):
        self._platform(monkeypatch, "IS_LINUX")
        monkeypatch.setenv("XDG_CACHE_HOME", "")
        assert mod._browsers_cache_dir() == tmp_path / "home" / ".cache" / "ms-playwright"

    def test_macos_uses_library_caches(self, monkeypatch, tmp_path):
        self._platform(monkeypatch, "IS_MACOS")
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "ignored"))
        expected = tmp_path / "home" / "Library" / "Caches" / "ms-playwright"
        assert mod._browsers_cache_dir() == expected

    def test_windows_falls_back_to_appdata_local_without_localappdata(self, monkeypatch, tmp_path):
        self._platform(monkeypatch, "IS_WINDOWS")
        expected = tmp_path / "home" / "AppData" / "Local" / "ms-playwright"
        assert mod._browsers_cache_dir() == expected
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
        assert mod._browsers_cache_dir() == tmp_path / "local" / "ms-playwright"

    def test_an_unknown_platform_is_unknown_not_missing(self, monkeypatch):
        self._platform(monkeypatch, "none")
        assert mod._browsers_cache_dir() is None
        assert set(mod.browser_status().values()) == {mod.STATUS_UNKNOWN}

    def test_an_absolute_override_is_used_as_is(self, monkeypatch, tmp_path):
        self._platform(monkeypatch, "IS_LINUX")
        monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "custom"))
        assert mod._browsers_cache_dir() == tmp_path / "custom"

    def test_the_npm_config_projection_is_read_like_playwright_does(self, monkeypatch, tmp_path):
        self._platform(monkeypatch, "IS_LINUX")
        monkeypatch.setenv("npm_config_playwright_browsers_path", str(tmp_path / "npmcfg"))
        assert mod._browsers_cache_dir() == tmp_path / "npmcfg"

    def test_a_relative_override_resolves_against_the_working_directory(
        self, monkeypatch, tmp_path
    ):
        self._platform(monkeypatch, "IS_LINUX")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "rel/browsers")
        assert mod._browsers_cache_dir() == tmp_path / "rel" / "browsers"
        monkeypatch.setenv("INIT_CWD", str(tmp_path / "init"))
        assert mod._browsers_cache_dir() == tmp_path / "init" / "rel" / "browsers"

    def test_zero_means_the_serving_core_packages_local_browsers(self, monkeypatch, tmp_path):
        self._platform(monkeypatch, "IS_LINUX")
        core = tmp_path / "node_modules" / "playwright-core"
        core.mkdir(parents=True)
        (core / "browsers.json").write_text('{"browsers": []}')
        monkeypatch.setattr(mod, "_browsers_manifest_path", lambda: core / "browsers.json")
        monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "0")
        assert mod._browsers_cache_dir() == core / ".local-browsers"

    def test_zero_without_an_attributable_core_is_unknown(self, monkeypatch):
        self._platform(monkeypatch, "IS_LINUX")
        monkeypatch.setattr(mod, "_browsers_manifest_path", lambda: None)
        monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "0")
        assert mod._browsers_cache_dir() is None
        assert set(mod.browser_status().values()) == {mod.STATUS_UNKNOWN}


class TestBrowserStatus:
    """``downloaded`` / ``missing`` / ``unknown`` are read without launching anything."""

    @staticmethod
    def _require(monkeypatch, revisions: dict[str, str]) -> None:
        monkeypatch.setattr(mod, "_required_revisions", lambda: dict(revisions))
        monkeypatch.setattr(mod, "_revision_overrides", lambda: {})

    def test_a_directory_without_the_completion_marker_is_missing(
        self, monkeypatch, isolated_browser_cache
    ):
        self._require(monkeypatch, {"chromium": "1232"})
        (isolated_browser_cache / "chromium-1232" / "chrome-linux64").mkdir(parents=True)
        status = mod.browser_status()
        assert status["chromium"] == mod.STATUS_MISSING
        assert mod.detect()["browser_ok"] is False
        _complete(isolated_browser_cache / "chromium-1232")
        assert mod.browser_status()["chromium"] == mod.STATUS_DOWNLOADED

    def test_the_presence_fallback_also_requires_the_marker(self, isolated_browser_cache):
        (isolated_browser_cache / "firefox-1500").mkdir()
        assert mod.browser_status()["firefox"] == mod.STATUS_MISSING

    def test_the_presence_fallback_does_not_count_the_headless_shell(self, isolated_browser_cache):
        _complete(isolated_browser_cache / "chromium_headless_shell-1232")
        assert mod.browser_status()["chromium"] == mod.STATUS_MISSING

    def test_the_presence_fallback_counts_a_complete_special_build(self, isolated_browser_cache):
        # No revision is attributable (the fixture's default), and the only WebKit
        # in the cache is the override directory playwright-core wrote for this
        # host. Presence-only means it counts; a `missing` here would be permanent.
        (isolated_browser_cache / "webkit_mac14_special-2251").mkdir()
        assert mod.browser_status()["webkit"] == mod.STATUS_MISSING
        _complete(isolated_browser_cache / "webkit_mac14_special-2251")
        assert mod.browser_status()["webkit"] == mod.STATUS_DOWNLOADED
        assert mod.browsers_present()["webkit"] is True

    def test_an_absent_cache_directory_is_a_confirmed_absence(self, monkeypatch, tmp_path):
        monkeypatch.setattr(mod, "_browsers_cache_dir", lambda: tmp_path / "never-created")
        assert set(mod.browser_status().values()) == {mod.STATUS_MISSING}

    def test_an_unreadable_cache_is_unknown(self, monkeypatch, isolated_browser_cache):
        def refuse(self):
            raise PermissionError("denied")

        monkeypatch.setattr(mod.Path, "iterdir", refuse)
        assert set(mod.browser_status().values()) == {mod.STATUS_UNKNOWN}
        assert mod.detect()["browsers"] == {"chromium": False, "firefox": False, "webkit": False}

    def test_an_unreadable_marker_is_unknown(self, monkeypatch, isolated_browser_cache):
        self._require(monkeypatch, {"chromium": "1232"})
        (isolated_browser_cache / "chromium-1232").mkdir()
        real_is_file = mod.Path.is_file

        def is_file(self):
            if self.name == mod._INSTALLATION_MARKER:
                raise PermissionError("denied")
            return real_is_file(self)

        monkeypatch.setattr(mod.Path, "is_file", is_file)
        assert mod.browser_status()["chromium"] == mod.STATUS_UNKNOWN

    def test_a_host_override_revision_is_matched_by_its_special_directory(
        self, monkeypatch, isolated_browser_cache
    ):
        monkeypatch.setattr(mod, "_required_revisions", lambda: {"webkit": "2365"})
        monkeypatch.setattr(mod, "_revision_overrides", lambda: {"webkit": {"mac14": "2251"}})
        monkeypatch.setattr(mod, "_playwright_host_platform", lambda: "mac14")
        _complete(isolated_browser_cache / "webkit_mac14_special-2251")
        assert mod.browser_status()["webkit"] == mod.STATUS_DOWNLOADED

    def test_a_complete_plain_build_on_a_host_the_override_names_reads_unknown_not_missing(
        self, monkeypatch, isolated_browser_cache
    ):
        # The port says this host takes the special build, yet the installer wrote
        # and completed the plain required-revision directory. Only playwright-core
        # decides which directory it writes, so this is evidence the port is wrong
        # about the host, not that the cache is empty.
        monkeypatch.setattr(mod, "_required_revisions", lambda: {"webkit": "2365"})
        monkeypatch.setattr(
            mod, "_revision_overrides", lambda: {"webkit": {"ubuntu20.04-x64": "2092"}}
        )
        monkeypatch.setattr(mod, "_playwright_host_platform", lambda: "ubuntu20.04-x64")
        _complete(isolated_browser_cache / "webkit-2365")
        assert mod.browser_status()["webkit"] == mod.STATUS_UNKNOWN
        assert mod.browsers_present()["webkit"] is False
        # An incomplete special directory beside it (an interrupted download of
        # the build the port predicted) does not turn the complete plain one into
        # a confident `missing` either.
        (isolated_browser_cache / "webkit_ubuntu20.04-x64_special-2092").mkdir()
        assert mod.browser_status()["webkit"] == mod.STATUS_UNKNOWN

    def test_an_incomplete_plain_build_on_a_host_the_override_names_is_still_missing(
        self, monkeypatch, isolated_browser_cache
    ):
        monkeypatch.setattr(mod, "_required_revisions", lambda: {"webkit": "2365"})
        monkeypatch.setattr(
            mod, "_revision_overrides", lambda: {"webkit": {"ubuntu20.04-x64": "2092"}}
        )
        monkeypatch.setattr(mod, "_playwright_host_platform", lambda: "ubuntu20.04-x64")
        (isolated_browser_cache / "webkit-2365").mkdir()
        assert mod.browser_status()["webkit"] == mod.STATUS_MISSING
        # A plain build of the WRONG revision is not evidence about this host.
        _complete(isolated_browser_cache / "webkit-2300")
        assert mod.browser_status()["webkit"] == mod.STATUS_MISSING

    def test_a_complete_special_build_for_another_host_reads_unknown_not_missing(
        self, monkeypatch, isolated_browser_cache
    ):
        # Special directories exist only where playwright-core's own platform
        # logic put them, so one this host key did not predict says the port
        # may be stale, not that the cache is empty.
        monkeypatch.setattr(mod, "_required_revisions", lambda: {"webkit": "2365"})
        monkeypatch.setattr(
            mod, "_revision_overrides", lambda: {"webkit": {"ubuntu20.04-x64": "2092"}}
        )
        monkeypatch.setattr(mod, "_playwright_host_platform", lambda: "ubuntu24.04-x64")
        _complete(isolated_browser_cache / "webkit_ubuntu20.04-x64_special-2092")
        assert mod.browser_status()["webkit"] == mod.STATUS_UNKNOWN
        assert mod.browsers_present()["webkit"] is False

    def test_an_unmatched_special_build_also_reads_unknown_when_this_host_wanted_a_special(
        self, monkeypatch, isolated_browser_cache
    ):
        monkeypatch.setattr(mod, "_required_revisions", lambda: {"webkit": "2365"})
        monkeypatch.setattr(
            mod,
            "_revision_overrides",
            lambda: {"webkit": {"ubuntu20.04-x64": "2092", "debian11-x64": "2092"}},
        )
        monkeypatch.setattr(mod, "_playwright_host_platform", lambda: "debian11-x64")
        _complete(isolated_browser_cache / "webkit_ubuntu20.04-x64_special-2092")
        assert mod.browser_status()["webkit"] == mod.STATUS_UNKNOWN

    def test_an_incomplete_special_build_for_another_host_is_still_missing(
        self, monkeypatch, isolated_browser_cache
    ):
        monkeypatch.setattr(mod, "_required_revisions", lambda: {"webkit": "2365"})
        monkeypatch.setattr(
            mod, "_revision_overrides", lambda: {"webkit": {"ubuntu20.04-x64": "2092"}}
        )
        monkeypatch.setattr(mod, "_playwright_host_platform", lambda: "ubuntu24.04-x64")
        (isolated_browser_cache / "webkit_ubuntu20.04-x64_special-2092").mkdir()
        assert mod.browser_status()["webkit"] == mod.STATUS_MISSING

    def test_a_special_build_of_one_engine_does_not_make_another_unknown(
        self, monkeypatch, isolated_browser_cache
    ):
        self._require(monkeypatch, {"chromium": "1232", "webkit": "2365"})
        _complete(isolated_browser_cache / "webkit_ubuntu20.04-x64_special-2092")
        status = mod.browser_status()
        assert status["chromium"] == mod.STATUS_MISSING
        assert status["webkit"] == mod.STATUS_UNKNOWN

    def test_a_complete_plain_build_counts_on_a_host_no_override_names(
        self, monkeypatch, isolated_browser_cache
    ):
        # An override for some other platform must not demote a complete build.
        monkeypatch.setattr(mod, "_required_revisions", lambda: {"webkit": "2365"})
        monkeypatch.setattr(
            mod,
            "_revision_overrides",
            lambda: {"webkit": {"mac14": "2251", "ubuntu20.04-x64": "2092"}},
        )
        _complete(isolated_browser_cache / "webkit-2365")
        for host in ("mac15-arm64", "ubuntu24.04-x64", "win64"):
            monkeypatch.setattr(mod, "_playwright_host_platform", lambda h=host: h)
            assert mod.browser_status()["webkit"] == mod.STATUS_DOWNLOADED

    def test_detect_reports_status_beside_the_boolean_projection(
        self, monkeypatch, isolated_browser_cache
    ):
        self._require(monkeypatch, {"chromium": "1232", "firefox": "1549", "webkit": "2365"})
        _complete(isolated_browser_cache / "chromium-1232")
        (isolated_browser_cache / "firefox-1549").mkdir()
        result = mod.detect()
        assert result["browser_status"] == {
            "chromium": "downloaded",
            "firefox": "missing",
            "webkit": "missing",
        }
        assert result["browsers"] == {"chromium": True, "firefox": False, "webkit": False}
        assert result["browser_ok"] is True


_POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")


def _pid_gone(pid: int, deadline_s: float = 5.0) -> bool:
    """Whether *pid* has exited (a zombie awaiting its reaper counts as exited)."""
    import time

    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        with contextlib.suppress(OSError):
            stat_line = Path(f"/proc/{pid}/stat").read_text()
            if stat_line.rsplit(")", 1)[1].split()[0] == "Z":
                return True
        time.sleep(0.05)
    return False


def _tree_script(pid_file: Path) -> list[str]:
    """A child that starts a grandchild and records its pid and start identity."""
    import sys

    code = (
        "import subprocess, sys, time\n"
        "from kiro_crew import platform_compat\n"
        "g = subprocess.Popen(\n"
        "    [sys.executable, '-c', 'import time; time.sleep(60)'],\n"
        f"    cwd={str(pid_file.parent)!r},\n"
        ")\n"
        "start_id = platform_compat.get_process_start_id(g.pid)\n"
        f"open({str(pid_file)!r}, 'w').write(f'{{g.pid}}:{{start_id or \"\"}}')\n"
        "time.sleep(60)\n"
    )
    return [sys.executable, "-c", code]


def _read_pid_identity(pid_file: Path) -> tuple[int, str | None]:
    """Read the child-captured grandchild pid and stable start identity."""
    pid_text, start_id = pid_file.read_text().split(":", 1)
    return int(pid_text), start_id or None


class TestInstallerChildrenAreTerminable:
    """A timeout or a gateway shutdown ends the installer's whole process tree."""

    @_POSIX_ONLY
    def test_a_timeout_kills_the_grandchild_too(self, tmp_path):
        import signal

        pid_file = tmp_path / "grandchild.pid"
        pid: int | None = None
        start_id: str | None = None
        try:
            rc, _out, err = mod._run(_tree_script(pid_file), 3.0, cwd=str(tmp_path))
            assert rc == 124
            assert "timed out" in err
            assert pid_file.exists()
            pid, start_id = _read_pid_identity(pid_file)
            assert _pid_gone(pid)
        finally:
            if (
                pid is not None
                and start_id is not None
                and mod.platform_compat.get_process_start_id(pid) == start_id
            ):
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)
                _pid_gone(pid)

    @_POSIX_ONLY
    def test_terminating_the_scope_kills_a_running_child_tree(self, tmp_path):
        import signal
        import threading
        import time

        pid_file = tmp_path / "grandchild.pid"
        pid: int | None = None
        start_id: str | None = None
        scope = mod.InstallScope()
        outcome: list[tuple[int, str, str]] = []
        worker = threading.Thread(
            target=lambda: outcome.append(
                mod.run_in_scope(
                    scope,
                    mod._run,
                    _tree_script(pid_file),
                    60.0,
                    cwd=str(tmp_path),
                )
            )
        )
        started = time.monotonic()
        try:
            worker.start()
            while not pid_file.exists() and time.monotonic() - started < 10:
                time.sleep(0.05)
            assert pid_file.exists(), "child never started"
            pid, start_id = _read_pid_identity(pid_file)

            assert scope.terminate() == 1
            worker.join(10)
            assert not worker.is_alive()
            assert outcome and outcome[0][0] == mod.INTERRUPTED_RC
            assert time.monotonic() - started < 30
            assert _pid_gone(pid)
        finally:
            scope.terminate()
            if worker.ident is not None:
                worker.join(10)
            if (
                pid is not None
                and start_id is not None
                and mod.platform_compat.get_process_start_id(pid) == start_id
            ):
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)
                _pid_gone(pid)

    def test_a_terminated_scope_spawns_nothing(self, monkeypatch):
        spawned: list[object] = []
        monkeypatch.setattr(mod.subprocess, "Popen", lambda *a, **k: spawned.append(a))
        scope = mod.InstallScope()
        scope.terminate()

        rc, _out, err = mod.run_in_scope(scope, mod._run, ["npm", "install"], 10.0)

        assert rc == mod.INTERRUPTED_RC
        assert "interrupted" in err
        assert spawned == []

    def test_the_scope_is_thread_local_and_restored(self):
        scope = mod.InstallScope()
        assert mod._current_scope() is None
        assert mod.run_in_scope(scope, mod._current_scope) is scope
        assert mod._current_scope() is None

    def test_a_child_outside_any_scope_is_not_adopted(self, tmp_path):
        import sys

        rc, out, _err = mod._run([sys.executable, "-c", "print('ok')"], 20.0)
        assert (rc, out.strip()) == (0, "ok")


class TestPlaywrightHostPlatform:
    """The port of playwright-core's ``calculatePlatform``."""

    def _host(self, monkeypatch, *, system, release="", machine="x86_64", os_release=None):
        monkeypatch.delenv("PLAYWRIGHT_HOST_PLATFORM_OVERRIDE", raising=False)
        monkeypatch.setattr(mod.platform_compat, "IS_MACOS", system == "mac")
        monkeypatch.setattr(mod.platform_compat, "IS_LINUX", system == "linux")
        monkeypatch.setattr(mod.platform_compat, "IS_WINDOWS", system == "win")
        monkeypatch.setattr(mod.platform, "release", lambda: release)
        monkeypatch.setattr(mod.platform, "machine", lambda: machine)

        def fake_os_release():
            if os_release is None:
                raise OSError("absent")
            return os_release

        monkeypatch.setattr(mod.platform, "freedesktop_os_release", fake_os_release)
        return mod._playwright_host_platform()

    def test_the_environment_override_wins(self, monkeypatch):
        monkeypatch.setenv("PLAYWRIGHT_HOST_PLATFORM_OVERRIDE", "debian11-x64")
        assert mod._playwright_host_platform() == "debian11-x64"

    def test_macos_keys(self, monkeypatch):
        assert self._host(monkeypatch, system="mac", release="17.7.0") == "mac10.13"
        assert self._host(monkeypatch, system="mac", release="19.6.0") == "mac10.15"
        assert self._host(monkeypatch, system="mac", release="23.4.0") == "mac14"
        assert (
            self._host(monkeypatch, system="mac", release="23.4.0", machine="arm64")
            == "mac14-arm64"
        )
        # Clamped at the last stable major, as Playwright does.
        assert self._host(monkeypatch, system="mac", release="30.0.0") == "mac15"

    def test_windows_is_win64(self, monkeypatch):
        assert self._host(monkeypatch, system="win") == "win64"

    def test_linux_keys(self, monkeypatch):
        def linux(distro, version, machine="x86_64"):
            return self._host(
                monkeypatch,
                system="linux",
                machine=machine,
                os_release={"ID": distro, "VERSION_ID": version},
            )

        assert linux("ubuntu", "20.04") == "ubuntu20.04-x64"
        assert linux("ubuntu", "22.04", "aarch64") == "ubuntu22.04-arm64"
        assert linux("ubuntu", "26.04") == "ubuntu26.04-x64"
        assert linux("linuxmint", "21") == "ubuntu22.04-x64"
        assert linux("debian", "11") == "debian11-x64"
        assert linux("debian", "10") == "ubuntu24.04-x64"
        assert linux("fedora", "42") == "ubuntu24.04-x64"

    def test_an_unsupported_linux_arch_is_unknown(self, monkeypatch):
        assert self._host(monkeypatch, system="linux", machine="riscv64") == "<unknown>"

    def test_no_os_release_falls_back_like_playwright(self, monkeypatch):
        assert self._host(monkeypatch, system="linux") == "ubuntu24.04-x64"
