"""Tests for the Notes (md-notebook) builtin backend.

Ported from the app's Node suite (``backend/test/server.test.mjs`` plus the
core-git and core-notes package tests), with added coverage for the proxy-HMAC
gate that the external app did not have.

Every request is signed the way the gateway signs it, so the auth middleware is
exercised on each call rather than bypassed. The folder picker is disabled via
``MD_NOTEBOOK_NO_PICKER`` so no GUI dialog can ever open during a run.

This file holds request signing and the proxy-HMAC gate, the vault routes and the basic
note read, save and delete routes. The other topics live in ``test_md_notebook_notes.py``,
``test_md_notebook_git_trust.py``, ``test_md_notebook_write_safety.py`` and
``test_md_notebook_settings_sync.py``, and the shared fixtures in ``md_notebook_helpers.py``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest
import yarl
from aiohttp.test_utils import TestClient, TestServer
from md_notebook_helpers import _seed_template  # noqa: F401 - fixture
from md_notebook_helpers import fixtures_fixture  # noqa: F401 - fixture
from md_notebook_helpers import SECRET, _clone, _git, signed_client
from windows_sim import replace_sharing_violation

from conftest import requires_symlinks
from kiro_crew import atomic_write as atomic_write_mod
from kiro_crew import platform_compat
from kiro_crew.apps.builtins.md_notebook import git_ops

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
    """Sign exactly as the gateway does (apps/routes_proxy.py::handle_app_api_proxy):
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
        status, _ = await client.post("/api/vaults", {"url": str(remote), "subfolder": "/etc"})
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
async def test_sync_on_a_local_only_vault_commits_without_pushing(fixtures, tmp_path: Path) -> None:
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
        assert (await client.post("/api/note/move", {"from": ".git/config", "to": "x.md"}))[
            0
        ] == 400
        assert (await client.post("/api/note/move", {"from": "One.md", "to": ".git/config"}))[
            0
        ] == 400
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

    facade = Path(server_mod.__file__)
    source = "".join(
        path.read_text(encoding="utf-8") for path in (facade, *facade.parent.glob("server_*.py"))
    )
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
async def test_trash_open_of_a_scoped_vault_stays_inside_the_scope(fixtures, monkeypatch) -> None:
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
        assert (await client.put("/api/note", {"path": "One.md", "content": "# One\n\nedited\n"}))[
            0
        ] == 200

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
        assert (await client.put("/api/note", {"path": "One.md", "content": "# One\n\ntimed\n"}))[
            0
        ] == 200

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
            await client.put(
                "/api/note", {"path": "Two.md", "content": "---\ntitle: Two\n---\n\nx\n"}
            )
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
        assert not exclude.exists() or git_ops.TRASH_DIR not in exclude.read_text(encoding="utf-8")
        assert (await client.put("/api/note", {"path": "One.md", "content": "# One\n\nedit\n"}))[
            0
        ] == 200

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
    # Imported before the env moves and never reloaded: the property is that a change
    # AFTER import is honoured, so no path may be captured when the module loads.
    from kiro_crew.apps.builtins.md_notebook import server as server_mod

    monkeypatch.setattr(server_mod, "_HOME", None)
    monkeypatch.setenv("KIROCREW_HOME", str(crew))
    monkeypatch.setenv("MD_NOTEBOOK_HOME", str(stray))
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


def test_vault_registry_commit_retries_windows_sharing_violation(fixtures, monkeypatch) -> None:
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

        # Simulate an external edit after the read. The guard compares float ms
        # (st_mtime * 1000) within MTIME_TOLERANCE_MS, and one filesystem clock tick
        # (NTFS ~15.6 ms, HFS+ 1 s) can give this write the read's own mtime, so the
        # edit is stamped strictly newer rather than slept apart.
        target = Path(vault["localPath"]) / "One.md"
        target.write_text("# One\n\nchanged by another program\n", encoding="utf-8")
        newer_ns = round(stale_mtime * 1_000_000) + 5_000_000_000
        os.utime(target, ns=(newer_ns, newer_ns))
        assert abs(target.stat().st_mtime * 1000 - stale_mtime) > _mod.MTIME_TOLERANCE_MS

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
        status, _ = await client.put("/api/note", {"path": "../escape.md", "content": "nope"})
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
        status, body = await client.post("/api/note/move", {"from": "One.md", "to": "dst.md"})
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

    `yaml.safe_load` turns an unquoted date into a `datetime.date`, which
    `json.dumps` rejects, so the read coerces the metadata to a string.
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
