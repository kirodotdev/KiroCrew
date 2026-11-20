"""Every spawn that runs app-authored text hides the gateway's secret files.

An app's lifecycle script, its ``onInstall`` script, its build step, its
``detectInstalled`` probe (two call sites) and its ``openCommand`` all run text or third-party
code that comes from the app. ``.local_secret`` and ``run/gateway-<port>.secret`` hold the one
credential of the gateway's internal routes, and the ``strict`` tier the detect probes run in
leaves both readable (``run`` and ``.local_secret`` are crew-home leaves that stay visible).

Each test drives one call site and checks only the ``extra_hidden_dirs`` its sandbox wrap
received. No sandbox and no process runs.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from apps_registry_coverage_helpers import _asgi_backend  # noqa: F401
from apps_registry_coverage_helpers import pip_importable_fixture  # noqa: F401
from apps_registry_helpers import _catalog_absent  # noqa: F401 - fixture
from apps_registry_helpers import _explicit_registry_execution_admission  # noqa: F401 - fixture
from apps_registry_helpers import _install_script_runs_on_this_host  # noqa: F401 - fixture
from apps_registry_helpers import unsandboxed_spawn  # noqa: F401 - fixture
from test_s29_spawn_env_regression import open_app_spawn

from kiro_crew.apps import lifecycle_scripts, registry
from kiro_crew.apps import routes as routes_mod
from kiro_crew.apps.manifest import AppManifest
from kiro_crew.config.paths import config_dir


def _seed_secret_files() -> tuple[str, ...]:
    home = Path(config_dir())
    (home / "run").mkdir(parents=True, exist_ok=True)
    names = (".local_secret", "run/gateway-8765.secret")
    for name in names:
        (home / name).write_text("x", encoding="utf-8")
    return tuple(str(home / name) for name in names)


def _assert_hides(hidden: tuple[str, ...], secrets: tuple[str, ...], site: str) -> None:
    for path in secrets:
        assert path in hidden, f"{path} is readable by the {site}"


class _Proc:
    """A process that has already exited cleanly with no output."""

    pid = None  # skips the post-exit straggler reap
    returncode: int | None = None

    def __init__(self) -> None:
        self.stdout = _NoLines()

    async def communicate(self) -> tuple[bytes, bytes]:
        self.returncode = 0
        return b"", b""

    async def wait(self) -> int:
        self.returncode = 0
        return 0


class _NoLines:
    def __aiter__(self) -> "_NoLines":
        return self

    async def __anext__(self) -> bytes:
        raise StopAsyncIteration


def _spy_registry(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, list[str], tuple[str, ...]]]:
    """Record the argv and hidden paths each registry-side wrap received."""
    seen: list[tuple[str, list[str], tuple[str, ...]]] = []

    async def _wrap(argv: Any, **kwargs: Any) -> Any:
        seen.append(("wrap", list(argv), tuple(kwargs.get("extra_hidden_dirs") or ())))
        return list(argv), None

    async def _strict(argv: Any, **kwargs: Any) -> Any:
        seen.append(("strict", list(argv), tuple(kwargs.get("extra_hidden_dirs") or ())))
        return list(argv), dict(kwargs.get("env") or {}), None

    async def _spawn(*argv: str, **kwargs: Any) -> Any:
        return _Proc()

    monkeypatch.setattr(registry, "wrap_argv_async", _wrap)
    monkeypatch.setattr(registry, "sandboxed_spawn_argv_async", _strict)
    monkeypatch.setattr(registry, "cgroup_scope_argv", lambda argv: argv)
    monkeypatch.setattr(registry, "create_subprocess_limited", _spawn)
    return seen


@pytest.mark.asyncio
async def test_the_lifecycle_script_spawn_hides_every_secret_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    secrets = _seed_secret_files()
    (tmp_path / "demo-app").mkdir()
    hidden: list[tuple[str, ...]] = []

    def _wrap(argv: Any, **kwargs: Any) -> Any:
        hidden.append(tuple(kwargs.get("extra_hidden_dirs") or ()))
        return list(argv), None

    async def _spawn(*argv: str, **kwargs: Any) -> Any:
        return _Proc()

    monkeypatch.setattr(lifecycle_scripts, "apps_dir", lambda: tmp_path)
    monkeypatch.setattr(lifecycle_scripts, "app_execution_denied", lambda *a, **k: None)
    monkeypatch.setattr(lifecycle_scripts, "wrap_argv", _wrap)
    monkeypatch.setattr(lifecycle_scripts, "cgroup_scope_argv", lambda argv: argv)
    monkeypatch.setattr(lifecycle_scripts.platform_compat, "IS_POSIX", True)
    monkeypatch.setattr(lifecycle_scripts, "create_subprocess_limited", _spawn)

    await lifecycle_scripts.run_lifecycle_script("demo-app", "true")

    assert len(hidden) == 1, "the lifecycle script never reached the sandbox wrap"
    _assert_hides(hidden[0], secrets, "lifecycle script")


@pytest.mark.asyncio
async def test_the_install_script_spawn_hides_every_secret_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    secrets = _seed_secret_files()
    entry = {"name": "demoapp", "repo": "https://example.com/demo.git", "branch": "main"}
    monkeypatch.setattr(registry, "get_registry_app", lambda n: entry)
    monkeypatch.setattr(registry, "_entry_git_url", lambda e: "https://example.com/demo.git")

    async def _fake_manifest(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {}

    async def _fake_build(git_url: str, name: str, log_lines: Any, **kwargs: Any) -> Any:
        return {"ok": True, "pkg_dir": tmp_path}

    monkeypatch.setattr(registry, "_fetch_app_manifest", _fake_manifest)
    monkeypatch.setattr(registry, "app_admission_denied", lambda *a, **k: None)
    monkeypatch.setattr(registry, "sel", lambda: MagicMock())
    monkeypatch.setattr(registry, "_clone_build_app", _fake_build)
    (tmp_path / "app.json").write_text(
        json.dumps({"name": "demoapp", "setup": {"onInstall": "true"}}), encoding="utf-8"
    )
    seen = _spy_registry(monkeypatch)

    await registry.install_from_registry("demoapp")

    script = [h for kind, argv, h in seen if kind == "wrap" and argv[:2] == ["/bin/bash", "-c"]]
    assert len(script) == 1, seen
    _assert_hides(script[0], secrets, "onInstall script")


@pytest.mark.asyncio
async def test_the_app_build_spawn_hides_every_secret_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, pip_importable: None
) -> None:
    secrets = _seed_secret_files()
    (tmp_path / "requirements.txt").write_text("pytest\n", encoding="utf-8")
    seen = _spy_registry(monkeypatch)

    result = await registry._run_app_build(
        tmp_path, "demo", [], manifest=AppManifest.from_dict({}), self_managed=False
    )

    assert result == {"ok": True}
    build = [h for kind, argv, h in seen if kind == "wrap" and "pip" in argv]
    assert len(build) == 1, seen
    _assert_hides(build[0], secrets, "app build step")


@pytest.mark.asyncio
async def test_the_install_detect_probe_hides_every_secret_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secrets = _seed_secret_files()
    entry = {"name": "demoapp", "repo": "https://example.com/demo.git", "detectInstalled": "true"}
    monkeypatch.setattr(registry, "_manifest_shell_platform_refusal", lambda: None)
    monkeypatch.setattr(registry, "get_registry_app", lambda n: entry)
    monkeypatch.setattr(registry, "_entry_git_url", lambda e: "https://example.com/demo.git")

    async def _fake_manifest(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {}

    async def _stop(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"ok": False, "error": "stop-after-detect"}

    monkeypatch.setattr(registry, "_fetch_app_manifest", _fake_manifest)
    monkeypatch.setattr(registry, "app_admission_denied", lambda *a, **k: None)
    monkeypatch.setattr(registry, "sel", lambda: MagicMock())
    monkeypatch.setattr(registry, "_clone_build_app", _stop)
    seen = _spy_registry(monkeypatch)

    await registry.install_from_registry("demoapp")

    probe = [h for kind, argv, h in seen if kind == "strict" and argv[-1] == "true"]
    assert len(probe) == 1, seen
    _assert_hides(probe[0], secrets, "install detect probe")


@pytest.mark.asyncio
async def test_the_catalog_detect_probe_hides_every_secret_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secrets = _seed_secret_files()
    monkeypatch.setattr(registry, "app_execution_denied", lambda *a, **k: None)
    monkeypatch.setattr(registry, "_manifest_shell_platform_refusal", lambda: None)
    seen = _spy_registry(monkeypatch)

    await registry._detect_installed_probe([{"name": "demo-app", "detectInstalled": "true"}], {})

    probe = [h for kind, argv, h in seen if kind == "strict" and argv[-1] == "true"]
    assert len(probe) == 1, seen
    _assert_hides(probe[0], secrets, "catalog detect probe")


@pytest.mark.asyncio
async def test_the_open_command_spawn_hides_every_secret_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # ``open_app_spawn`` points the data home at this directory; seed it first.
    home = tmp_path / "kirocrew-home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    secrets = _seed_secret_files()
    hidden: list[tuple[str, ...]] = []

    async def _wrap(argv: Any, **kwargs: Any) -> Any:
        hidden.append(tuple(kwargs.get("extra_hidden_dirs") or ()))
        return list(argv), None

    monkeypatch.setattr(routes_mod, "wrap_argv_async", _wrap)

    await open_app_spawn(tmp_path, monkeypatch)

    assert len(hidden) == 1, "the open command never reached the sandbox wrap"
    _assert_hides(hidden[0], secrets, "open command")
