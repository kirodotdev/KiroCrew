"""Fakes and fixtures shared by the ``test_apps_registry_coverage*.py`` modules.

``_explicit_registry_execution_admission`` is autouse, so every module imports it by
name. The other fixtures are declared ``@pytest.fixture(name=...)`` and are imported
under their function names. The subprocess doubles fake the registry's own
chokepoints, so nothing here spawns git, npm or pip.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew.apps import registry
from kiro_crew.apps.manifest import AppManifest

#: The install's refusal for a root `data` the data directory cannot stand beside,
#: as `manager.gateway_data_dir_obstruction` spells it -- the transaction must carry it
#: unchanged, so it is pinned here as text.
_DATA_IS_A_FILE = (
    "`data` in the app tree is a file; Kiro Crew creates the app's data directory at "
    "that path and cannot install beside it."
)

# ---------------------------------------------------------------------------
# Fixtures / shared fakes
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- the registry coverage modules' autouse admission, moved verbatim; tracked for a de-flake pass
def _explicit_registry_execution_admission(monkeypatch):
    """These tests reach admitted registry code paths unless they say otherwise."""
    monkeypatch.setattr("kiro_crew.apps.execution.third_party_execution_allowed", lambda: True)


@pytest.fixture(name="cache_dir")
def cache_dir_fixture(tmp_path, monkeypatch):
    """Redirect the manifest cache to a temp directory (never the real home)."""
    cache = tmp_path / "cache" / "app-manifests"
    cache.mkdir(parents=True)
    monkeypatch.setattr(registry, "_manifest_cache_dir", lambda: cache)
    return cache


@pytest.fixture(name="pip_importable")
def pip_importable_fixture(monkeypatch):
    """Pin the gateway interpreter as one that HAS a ``pip`` module.

    ``_run_app_build`` decides whether to plan a Python build by probing
    ``importlib.util.find_spec("pip")`` on the RUNNING interpreter. That reads
    the host's own packaging, not the fixture tree: a venv created by ``uv`` or
    ``--without-pip`` has no ``pip`` module, so the branch soft-skips and every
    "the pip command is planned" assertion fails there while passing on a
    stdlib venv. Tests asserting the planned command take this fixture; the
    soft-skip contract is pinned separately with the opposite answer.
    """
    import site

    from kiro_crew import platform_compat

    monkeypatch.setattr(site, "ENABLE_USER_SITE", False)
    monkeypatch.setattr(platform_compat, "is_bundled_interpreter", lambda: False)
    real_find_spec = importlib.util.find_spec

    def _with_pip(name, *args, **kwargs):
        if name == "pip":
            return importlib.machinery.ModuleSpec("pip", loader=None)
        return real_find_spec(name, *args, **kwargs)

    monkeypatch.setattr(registry.importlib.util, "find_spec", _with_pip)


@pytest.fixture(name="bundled_interpreter")
def bundled_interpreter_fixture(monkeypatch, tmp_path):
    """Pin the gateway interpreter as the desktop app's BUNDLED one.

    The install-time Python gate forks on
    ``platform_compat.is_bundled_interpreter()`` — the single owner of the
    packaging-layout sentinel — so the fork is exercised by stubbing that
    function rather than by faking a bundle path per test.

    Also lays down the ``server.py`` that :func:`_asgi_backend`'s default
    manifest declares: the runtime provisions for a file-style entry only when
    the spawn would run it (a regular file inside the app root), so the file
    exists here and each case using it isolates the condition it is about. The
    case about a MISSING entry removes it explicitly.
    """
    from kiro_crew import platform_compat

    monkeypatch.setattr(platform_compat, "is_bundled_interpreter", lambda: True)
    (tmp_path / "server.py").write_text("", encoding="utf-8")


def _asgi_backend(entry: str = "server.py", **backend: Any) -> AppManifest:
    """A cloned manifest declaring an out-of-process backend entry point.

    Built through ``AppManifest.from_dict`` — the same normalization the runtime
    applies before the hook loaders ever see a manifest — because that is the view
    the install-time gate answers from.
    """
    return AppManifest.from_dict(
        {"name": "demo", "backend": {"entryPoint": entry, "type": "asgi", **backend}}
    )


class _FakeProc:
    """Minimal stand-in for ``asyncio.subprocess.Process``.

    *stdout_lines* makes ``proc.stdout`` async-iterable, which is what
    ``_run_app_build``'s drain loop consumes.
    """

    def __init__(
        self,
        returncode: int = 0,
        stdout_lines: list[bytes] | None = None,
        output: bytes = b"",
    ) -> None:
        self.returncode = returncode
        # nosemgrep: kirocrew.mock-pid-allocatable -- pre-existing literal pid on the fake process, moved verbatim; tracked for a de-flake pass
        self.pid = 31337
        self.kill_calls = 0
        self.wait_calls = 0
        self._output = output
        self.stdout = _AsyncLines(stdout_lines or [])

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._output, b""

    def kill(self) -> None:
        self.kill_calls += 1

    async def wait(self) -> int:
        self.wait_calls += 1
        return self.returncode or 0


class _AsyncLines:
    def __init__(self, lines: list[bytes]) -> None:
        self._lines = list(lines)

    def __aiter__(self):
        return self

    async def __anext__(self) -> bytes:
        if not self._lines:
            raise StopAsyncIteration
        return self._lines.pop(0)


def _fake_sandbox(monkeypatch, procs):
    """Neutralize the sandbox wrappers and hand out *procs* in order.

    Returns the list that each spawn's argv is appended to.
    """
    spawned: list[list[str]] = []
    queue = list(procs)

    async def _spawn(*argv, **kwargs):
        spawned.append(list(argv))
        return queue.pop(0) if queue else _FakeProc()

    monkeypatch.setattr(registry, "wrap_argv", lambda cmd, mode="": (list(cmd), None))
    monkeypatch.setattr(registry, "cgroup_scope_argv", lambda cmd: list(cmd))
    monkeypatch.setattr(registry, "create_subprocess_limited", _spawn)
    return spawned


def _reg(name: str, repo: str, branch: str = "main") -> SimpleNamespace:
    """A configured-registry stand-in with the fields the module reads."""
    return SimpleNamespace(name=name, repo=repo, branch=branch)


def _config_with(monkeypatch, registries: list[SimpleNamespace]) -> None:
    """Make every ``KiroCrewConfig.load()`` in this module see *registries*."""
    monkeypatch.setattr(
        "kiro_crew.config.loader.KiroCrewConfig.load",
        classmethod(lambda cls: SimpleNamespace(registries=registries)),
    )
