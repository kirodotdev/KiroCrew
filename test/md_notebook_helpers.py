"""Shared fixtures and helpers for the Notes (md-notebook) backend suites.

``test_md_notebook.py`` and its topic siblings (``test_md_notebook_notes.py``,
``test_md_notebook_git_trust.py``, ``test_md_notebook_write_safety.py`` and
``test_md_notebook_settings_sync.py``) import what they use from here by name. This is a
plain module, not a conftest, so each suite imports the fixtures it requests: ``fixtures``
(registered under that name by ``fixtures_fixture``, so a test's ``fixtures`` parameter does
not shadow the import) and the session-scoped ``_seed_template`` that ``fixtures`` depends on.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import shutil
import subprocess
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Optional

import pytest
import yarl
from aiohttp.test_utils import TestClient, TestServer
from git_maintenance_helpers import GIT_MAINTENANCE_OFF_CONFIG

from kiro_crew import platform_compat
from kiro_crew.loop_lock import LoopBoundLock
from kiro_crew.testing.wait import async_wait_until

SECRET = "test-proxy-secret"


"""Env that makes a git call hermetic regardless of fixture scope.

``test/conftest.py``'s ``_git_identity`` closes the same two host bleeds (identity,
and ``~/.gitconfig`` reaching in via e.g. ``core.excludesFile``), but it is autouse
and FUNCTION-scoped -- so the session-scoped template builder below runs before it and
would otherwise read the developer's real global config. Applied here explicitly
rather than widening that fixture's scope, which would stop it reverting per test.
"""
_GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
}


def _git(*args: str, cwd: Path) -> str:
    """Run git in a fixture repo, failing loudly on error."""
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        env={**os.environ, **_GIT_ENV},
    ).stdout.strip()


def _build_seed_repo(base: Path) -> None:
    """Create a bare remote plus a working checkout holding two notes, under *base*."""
    remote = base / "remote.git"
    _git("init", "--bare", "-b", "main", str(remote), cwd=base)
    seed = base / "seed"
    seed.mkdir()
    _git("init", "-b", "main", ".", cwd=seed)
    _git("config", "user.email", "test@example.invalid", cwd=seed)
    _git("config", "user.name", "Test", cwd=seed)
    # The notebook builds its own git env, and a push starts receive-pack in the
    # remote without ours, so both repos carry it (see git_maintenance_helpers).
    for key, value in GIT_MAINTENANCE_OFF_CONFIG:
        _git("config", key, value, cwd=remote)
        _git("config", key, value, cwd=seed)
    (seed / "One.md").write_text("# One\n\nlinks to [[Two]]\n", encoding="utf-8")
    (seed / "sub").mkdir()
    (seed / "sub" / "Two.md").write_text("---\ntitle: Two\n---\n\nbody #tag\n", encoding="utf-8")
    _git("add", "-A", cwd=seed)
    _git("commit", "-m", "seed", cwd=seed)
    _git("remote", "add", "origin", str(remote), cwd=seed)
    _git("push", "-u", "origin", "main", cwd=seed)


# Each module that imports this fixture holds its own session-scoped copy, so the
# template is built once per importing module on each worker.
@pytest.fixture(scope="session")
def _seed_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build the seed repo pair once per session; tests get a copy.

    ``_build_seed_repo`` spawns nine git subprocesses, and ``fixtures`` is
    function-scoped over 120 tests -- ~1.6s of setup each, which made this the most
    expensive file in the suite. Copying a prebuilt tree is ~5x cheaper.

    Session scope is safe because the template is never yielded to a test, only
    copied from, so no test can reach another's repo.
    """
    template = tmp_path_factory.mktemp("md-notebook-seed")
    _build_seed_repo(template)
    return template


def _seed_repo(base: Path, template: Path) -> tuple[Path, Path]:
    """Copy the session seed *template* into *base*, returning ``(remote, seed)``.

    ``git`` records the remote as an absolute path, so the copied checkout is
    re-pointed at the copied bare remote rather than the template's -- otherwise
    every test would push into one shared remote and see each other's commits.
    """
    remote, seed = base / "remote.git", base / "seed"
    shutil.copytree(template / "remote.git", remote)
    shutil.copytree(template / "seed", seed)
    _git("remote", "set-url", "origin", str(remote), cwd=seed)
    return remote, seed


class SignedClient:
    """Wraps a TestClient, signing each request as the gateway would."""

    def __init__(self, client: TestClient) -> None:
        self._client = client

    def _headers(self, method: str, target: str, body: bytes) -> dict[str, str]:
        ts = str(int(time.time()))
        digest = hashlib.sha256(body).hexdigest()
        msg = f"{ts}:{method}:{target}:{digest}"
        sig = hmac.new(SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()
        return {"X-KiroCrew-Proxy": f"{ts}:{sig}"}

    async def request(
        self, method: str, path: str, payload: Optional[dict[str, Any]] = None
    ) -> tuple[int, Any]:
        body = json.dumps(payload).encode() if payload is not None else b""
        # Sign the WIRE form of the target, as the gateway does: yarl requotes
        # the human-readable path (e.g. a space becomes %20), and raw_path_qs
        # is the exact request-target the client puts on the wire.
        url = yarl.URL(path)
        headers = self._headers(method, url.raw_path_qs, body)
        if payload is not None:
            headers["Content-Type"] = "application/json"
        resp = await self._client.request(method, url, data=body or None, headers=headers)
        try:
            return resp.status, await resp.json()
        except Exception:  # noqa: BLE001 — a non-JSON body is itself the failure
            return resp.status, await resp.text()

    async def get(self, path: str) -> tuple[int, Any]:
        return await self.request("GET", path)

    async def post(self, path: str, payload: Optional[dict] = None) -> tuple[int, Any]:
        return await self.request("POST", path, payload or {})

    async def put(self, path: str, payload: Optional[dict] = None) -> tuple[int, Any]:
        return await self.request("PUT", path, payload or {})

    async def delete(self, path: str) -> tuple[int, Any]:
        return await self.request("DELETE", path)


def _fresh_backend_state(server_mod, monkeypatch: pytest.MonkeyPatch) -> None:
    """Give one test the per-process backend state a new gateway starts with.

    These are the module globals the backend mutates at runtime (locks, caches, watches,
    the gh-token memo), plus the one by-value import a floor fixture patches on its
    source. Each is replaced through *monkeypatch*, so teardown hands back exactly the
    objects this test inherited. The module is never reloaded: a reload re-runs the whole
    body in the SHARED module and restores nothing, so every function, ``ApiError``'s
    identity and each by-value import would outlive the test. A runtime-mutable global
    added to server.py belongs in this list.
    """
    monkeypatch.setattr(server_mod, "_HOME", None)
    for name in ("_save_locks", "_vault_write_locks", "_caches", "_watches", "_self_writes"):
        monkeypatch.setattr(server_mod, name, {})
    monkeypatch.setattr(server_mod, "_vaults_lock", LoopBoundLock())
    monkeypatch.setattr(server_mod, "_settings_lock", LoopBoundLock())
    monkeypatch.setattr(server_mod, "_gh_cache", {"value": None, "at": 0.0})
    # server binds restrict_to_owner by value; follow the floor's live binding (Windows
    # stubs it on platform_compat) for this test only.
    monkeypatch.setattr(server_mod, "restrict_to_owner", platform_compat.restrict_to_owner)


@pytest.fixture(name="fixtures")
def fixtures_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _seed_template: Path):
    """The backend module with fresh per-process state, a temp home and git fixture repos."""
    monkeypatch.setenv("MD_NOTEBOOK_HOME", str(tmp_path / "home"))
    # The PAT lives under the crew data home (config_dir), never MD_NOTEBOOK_HOME,
    # so isolate KIROCREW_HOME too or tests would touch the real ~/.kiro/crew.
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "crew"))
    monkeypatch.setenv("KIROCREW_PROXY_SECRET", SECRET)
    monkeypatch.setenv("MD_NOTEBOOK_NO_PICKER", "1")
    # Every auth-bearing route falls through ``resolve_auth`` to ``gh_token``,
    # which spawns the host's real ``gh auth token`` whenever ``_find_gh`` finds
    # a trusted install. That makes the verdict depend on whether the developer
    # has gh installed and logged in, and can hand the backend under test a
    # live token. Pin the resolver's own override to a path that does not exist
    # so it answers "no gh" and the token path is exercised as a genuine absence.
    monkeypatch.setenv("MD_NOTEBOOK_GH_BIN", str(tmp_path / "no-such-gh"))
    from kiro_crew.apps.builtins.md_notebook import server as server_mod

    # Every data-home path resolves per call from the env above, so the module is
    # used as imported.
    _fresh_backend_state(server_mod, monkeypatch)
    remote, seed = _seed_repo(tmp_path, _seed_template)
    return server_mod, remote, seed


@asynccontextmanager
async def signed_client(server_mod) -> AsyncIterator[SignedClient]:
    """A signed test client for a backend module.

    An `async with` helper rather than an `@pytest_asyncio.fixture`, following
    the suite's convention for teardown that must await on the test's own loop.
    """
    app = server_mod.create_app()
    async with TestClient(TestServer(app)) as c:
        yield SignedClient(c)


async def _clone(client: SignedClient, remote: Path, **extra: Any) -> dict[str, Any]:
    status, body = await client.post("/api/vaults", {"url": str(remote), **extra})
    assert status == 200, body
    return body["vault"]


#: Lost-run ceiling for the suites' barriers that wait on a signal. Measured worst cases:
#: 0.21 s on an idle host, 11.9 s under a starved-host model (every executor job 0.2 s
#: late plus a GIL-hogging thread). Under half the 120 s test timeout, so a missed signal
#: fails here by name rather than as a killed worker.
_BARRIER_LOST_RUN_SEC = 30.0


async def _wait_for(predicate: Callable[[], Any], message: str, budget: float = 5.0) -> None:
    """Poll *predicate* until it is truthy under a lost-run bound."""
    await async_wait_until(predicate, describe=lambda: message)
