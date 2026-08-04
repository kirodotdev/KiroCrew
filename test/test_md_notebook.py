"""Tests for the Notes (md-notebook) builtin backend.

Ported from the app's Node suite (``backend/test/server.test.mjs`` plus the
core-git and core-notes package tests), with added coverage for the proxy-HMAC
gate that the external app did not have.

Every request is signed the way the gateway signs it, so the auth middleware is
exercised on each call rather than bypassed. The folder picker is disabled via
``MD_NOTEBOOK_NO_PICKER`` so no GUI dialog can ever open during a run.
"""
from __future__ import annotations

import hashlib
import hmac
import importlib
import json
import os
import shutil
import subprocess
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Optional

import pytest
from aiohttp.test_utils import TestClient, TestServer

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
        headers = self._headers(method, path, body)
        if payload is not None:
            headers["Content-Type"] = "application/json"
        resp = await self._client.request(method, path, data=body or None, headers=headers)
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
        for feature in ("createdAt", "attach", "saveGuard", "knowledge", "pickFolder"):
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
    # Vaults, by contrast, DO follow MD_NOTEBOOK_HOME.
    assert str(stray) in str(server_mod._vaults_json())


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
        # Frontmatter title wins; otherwise the filename is used.
        assert by_path["sub/Two.md"]["title"] == "Two"
        assert by_path["One.md"]["title"] == "One"
        assert by_path["One.md"]["createdAt"] > 0
        assert by_path["One.md"]["syncStatus"] == "synced"


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
        start = time.monotonic()
        status, body = await client.get("/api/notes")
        assert status == 200, body
        assert time.monotonic() - start < 5, "alias amplification was not contained"


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
        assert read["content"] == "# Untitled\n"


@pytest.mark.asyncio
async def test_new_note_in_folder(fixtures) -> None:
    _mod, remote, _seed = fixtures
    async with signed_client(_mod) as client:
        await _clone(client, remote)
        status, body = await client.post("/api/note/new", {"folder": "sub"})
        assert status == 200
        assert body["path"] == "sub/Untitled.md"


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
