"""Manifest shell strings are refused up front on native Windows.

``detectInstalled`` and ``openCommand`` run through ``/bin/sh -c``, and native
Windows has no ``/bin/sh``: a spawn there raises ``FileNotFoundError``, which the
registry probe and the install precheck would swallow as "not installed", and a
Windows desktop sets no ``DISPLAY``, so the open route would read it as a remote
host. Each site checks the platform before it spawns and names the reason. These
cases run on any host: they set
``platform_compat.IS_POSIX`` to False (or answer the refusal it produces) and
assert that no process starts.
"""

from __future__ import annotations

import logging
import platform as platform_mod
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from apps_registry_helpers import _catalog_absent  # noqa: F401 - fixture
from apps_registry_helpers import _explicit_registry_execution_admission  # noqa: F401 - fixture
from apps_routes_coverage_helpers import APP, _install, _make_app, _setup_env

import kiro_crew.apps.routes as routes_mod
from kiro_crew import platform_compat
from kiro_crew.apps import registry
from kiro_crew.apps.manager import enable_app


def _refuse_spawns(monkeypatch: pytest.MonkeyPatch, *modules: Any) -> list[tuple[Any, ...]]:
    """Make every spawn seam on *modules* record and fail, so a start is visible."""
    started: list[tuple[Any, ...]] = []

    async def _spawn(*argv: Any, **kwargs: Any) -> Any:
        started.append(argv)
        raise AssertionError(f"a process was started: {argv}")

    async def _sandboxed(argv: Any, *args: Any, **kwargs: Any) -> Any:
        started.append(tuple(argv))
        raise AssertionError(f"a probe reached the sandbox wrapper: {argv}")

    def _wrap(argv: Any, *args: Any, **kwargs: Any) -> Any:
        started.append(tuple(argv))
        raise AssertionError(f"a launch reached the sandbox wrapper: {argv}")

    for module in modules:
        monkeypatch.setattr(module, "create_subprocess_limited", _spawn)
        if module is registry:
            monkeypatch.setattr(module, "sandboxed_spawn_argv_async", _sandboxed)
        else:
            monkeypatch.setattr(module, "wrap_argv", _wrap)
    return started


def _as_native_windows(monkeypatch: pytest.MonkeyPatch, facade: Any) -> None:
    """Answer the platform refusal a native-Windows host gets, through *facade*.

    The flag itself is flipped only around the helper (the case below): the route
    and the install read files and build paths on the way to the check, and those
    must keep this host's own platform branches.
    """
    reason = registry.NATIVE_WINDOWS_SHELL_REFUSAL
    monkeypatch.setattr(facade, "_manifest_shell_platform_refusal", lambda: reason)


def test_the_refusal_is_native_windows_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform_compat, "IS_POSIX", True)
    assert registry._manifest_shell_platform_refusal() is None

    monkeypatch.setattr(platform_compat, "IS_POSIX", False)
    reason = registry._manifest_shell_platform_refusal()
    assert reason == registry.NATIVE_WINDOWS_SHELL_REFUSAL
    assert "native Windows" in reason
    assert "/bin/sh" in reason


@pytest.mark.asyncio
async def test_detect_probe_starts_nothing_and_warns_once_on_native_windows(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(platform_compat, "IS_POSIX", False)
    monkeypatch.setattr(registry, "app_execution_denied", lambda *a, **k: None)
    started = _refuse_spawns(monkeypatch, registry)
    entries = [
        {"name": "x", "detectInstalled": "exit 0"},
        {"name": "y", "detectInstalled": "exit 0"},
    ]

    with caplog.at_level(logging.WARNING):
        detected = await registry._detect_installed_probe(entries, {})

    assert detected == set()
    assert started == []
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, [r.getMessage() for r in warnings]
    message = warnings[0].getMessage()
    assert "native Windows" in message
    assert "x, y" in message


@pytest.mark.asyncio
async def test_detect_probe_does_not_warn_for_an_entry_it_would_not_probe(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Known, command-less and denied entries never reached the spawn: no warning."""
    monkeypatch.setattr(platform_compat, "IS_POSIX", False)
    monkeypatch.setattr(registry, "app_execution_denied", lambda name, **k: "denied")
    started = _refuse_spawns(monkeypatch, registry)
    entries = [
        {"name": "known", "detectInstalled": "exit 0"},
        {"name": "bare"},
        {"name": "denied", "detectInstalled": "exit 0"},
    ]

    with caplog.at_level(logging.WARNING):
        detected = await registry._detect_installed_probe(entries, {"known": {}})

    assert detected == set()
    assert started == []
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


@pytest.mark.asyncio
async def test_install_precheck_starts_no_probe_and_logs_the_reason_on_native_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _as_native_windows(monkeypatch, registry)
    entry = {
        "name": "demoapp",
        "repo": "https://example.com/demo.git",
        "detectInstalled": "exit 0",
    }
    monkeypatch.setattr(registry, "get_registry_app", lambda n: entry)
    monkeypatch.setattr(registry, "_entry_git_url", lambda e: "https://example.com/demo.git")

    async def _fake_manifest(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {}

    monkeypatch.setattr(registry, "_fetch_app_manifest", _fake_manifest)
    monkeypatch.setattr(registry, "app_admission_denied", lambda *a, **k: None)
    monkeypatch.setattr(registry, "app_execution_denied", lambda *a, **k: None)
    monkeypatch.setattr(registry, "sel", lambda: MagicMock())
    started = _refuse_spawns(monkeypatch, registry)
    reached_build: list[str] = []

    # The install goes on past the precheck, as it did on this host before; stop it
    # at the build so nothing else runs.
    async def _fake_build(git_url: str, name: str, log_lines: list[str], **kwargs: Any) -> Any:
        reached_build.append(name)
        return {"ok": False, "error": "stop-after-precheck"}

    monkeypatch.setattr(registry, "_clone_build_app", _fake_build)
    log_lines: list[str] = []

    result = await registry.install_from_registry("demoapp", log_lines)

    assert started == []
    assert reached_build == ["demoapp"]
    assert "already installed" not in str(result.get("error", ""))
    refusals = [line for line in log_lines if line.startswith("detectInstalled not run:")]
    assert refusals == [f"detectInstalled not run: {registry.NATIVE_WINDOWS_SHELL_REFUSAL}"]


@pytest.mark.asyncio
async def test_open_answers_409_and_starts_nothing_on_native_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_env(tmp_path, monkeypatch)
    _install(tmp_path, openCommand="open-my-app")
    enable_app(APP)
    monkeypatch.setattr(routes_mod, "app_execution_denied", lambda n, **k: None)
    # A Windows desktop: no DISPLAY and not macOS, which the display test reads as remote.
    monkeypatch.setattr(platform_mod, "system", lambda: "Windows")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    started = _refuse_spawns(monkeypatch, routes_mod)
    audit = MagicMock()
    monkeypatch.setattr(routes_mod, "sel", lambda: audit)
    _as_native_windows(monkeypatch, routes_mod)

    async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
        resp = await client.post(f"/api/apps/{APP}/open")
        status = resp.status
        body = await resp.json()

    assert status == 409
    assert body["code"] == "app_open_unsupported_platform"
    assert "native Windows" in body["error"]
    assert "remote" not in body
    assert "command" not in body
    assert started == []
    events = [
        c.kwargs
        for c in audit.log_api_access.call_args_list
        if c.kwargs.get("operation") == "app_open"
    ]
    assert [e["outcome"] for e in events] == ["denied"]


@pytest.mark.asyncio
async def test_open_trust_denial_still_answers_first_on_native_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The consent affordance keys off ``app_execution_denied``: it keeps priority."""
    _setup_env(tmp_path, monkeypatch)
    _install(tmp_path, openCommand="open-my-app")
    enable_app(APP)
    monkeypatch.setattr(
        routes_mod,
        "app_execution_denied",
        lambda n, **k: "third-party app execution is not admitted",
    )
    started = _refuse_spawns(monkeypatch, routes_mod)
    _as_native_windows(monkeypatch, routes_mod)

    async with TestClient(TestServer(_make_app(dashboard_user="owner"))) as client:
        resp = await client.post(f"/api/apps/{APP}/open")
        status = resp.status
        body = await resp.json()

    assert status == 403
    assert body["code"] == "app_execution_denied"
    assert started == []
