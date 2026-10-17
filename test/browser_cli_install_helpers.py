"""Autouse pins, real-implementation captures and fakes shared by the browser CLI tests.

``test_browser_cli_install.py`` and its ``_browser_revisions``, ``_gateway_command`` and
``_run_control`` siblings import these by name. The three fixtures are autouse, so every
one of them clears the Node-tool caches, keeps ``browser_ok`` off the real Playwright
cache and runs on a host with no OS-library remedy. ``_wire`` fakes tool resolution and
the subprocess layer; ``_complete`` builds a finished browser download. The fixture
docstrings describe the tests of the modules that import them.
"""

from __future__ import annotations

import contextlib
from pathlib import Path

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


@pytest.fixture(name="isolated_browser_cache", autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- no test in the test_browser_cli_install*.py modules calls monkeypatch.undo()
def isolated_browser_cache_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
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
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- no test in the test_browser_cli_install*.py modules calls monkeypatch.undo()
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
