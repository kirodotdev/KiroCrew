"""Notes (md-notebook) backend: the git trust boundary.

Covers the GitHub token routes, the knowledge-flag and folder-picker routes, and sync
across the trust boundary: remote, push-URL and gitdir pins, subfolder and ``safe_join``
containment, remote-URL and ref validation, the sensitive-path index gate, neutralized
repository config and the repository-supplied driver probes. Shared fixtures live in
``md_notebook_helpers.py``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Optional

import pytest
from md_notebook_helpers import _seed_template  # noqa: F401 - fixture
from md_notebook_helpers import fixtures_fixture  # noqa: F401 - fixture
from md_notebook_helpers import _clone, _git, signed_client

from kiro_crew.apps.builtins.md_notebook import git_ops

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
    """A clone that fails must not overwrite an already-stored, valid PAT with
    the (possibly bad) token submitted alongside the failing request."""
    server_mod, _remote, _seed = fixtures
    async with signed_client(server_mod) as client:
        assert (await client.put("/api/pat", {"pat": "ghp_good_existing"}))[0] == 200
        # Clone a bogus URL with a different token; the clone fails.
        # flake-ok: pre-existing clone of a reserved .invalid host, which never resolves, so the clone fails without traffic; moved verbatim
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
        _git(
            "config", "--add", "remote.origin.pushurl", "https://evil.invalid/x.git", cwd=Path(root)
        )
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
            root,
            branch=vault.get("branch"),
            trusted_remote=vault["remoteUrl"],
            trusted_gitdir=gitdir,
        )
        assert "conflicts" in res

        # A git dir that does not match (redirected .git) must refuse.
        with pytest.raises(git_ops.GitError):
            await git_ops.sync(
                root,
                branch=vault.get("branch"),
                trusted_remote=vault["remoteUrl"],
                trusted_gitdir="/tmp/some-other-gitdir",
            )


@pytest.mark.asyncio
async def test_knowledge_unknown_vault(fixtures) -> None:
    server_mod, _remote, _seed = fixtures
    async with signed_client(server_mod) as client:
        status, _ = await client.put("/api/vaults/knowledge", {"vault": "nope", "knowledge": True})
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
# Subfolder path traversal must not escape the vault
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
        _git(
            "remote",
            "set-url",
            "origin",
            str(remote.parent / "gone.git"),
            cwd=Path(vault["localPath"]),
        )

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
def test_safe_join_rejects_escapes_before_touching_the_filesystem(
    fixtures, tmp_path: Path, rel: str
) -> None:
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
async def test_index_skips_files_the_sensitive_path_gate_rejects(
    fixtures, monkeypatch: pytest.MonkeyPatch
) -> None:
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

        status, body = await client.get(
            f"/api/search?q=borrowed-credential-body&vault={vault['id']}"
        )
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
async def test_probe_allows_worktree_extension_without_config_file(fixtures) -> None:
    """`extensions.worktreeConfig=true` with no `config.worktree` on disk is a
    healthy EMPTY scope (git creates the file lazily). Probing it anyway exits
    128 with a non-empty stderr, which reads as "unprobeable config" for a
    filter-free vault."""
    server_mod, remote, _seed = fixtures
    async with signed_client(server_mod) as client:
        vault = await _clone(client, remote)
        root = Path(vault["localPath"])
        _git("config", "--local", "extensions.worktreeConfig", "true", cwd=root)
        assert not (root / ".git" / "config.worktree").exists()
        assert await git_ops.repo_supplied_driver(str(root)) == ""


@pytest.mark.asyncio
async def test_probe_still_refuses_worktree_scoped_driver(fixtures) -> None:
    """The gate narrows WHEN the `--worktree` scope is probed, never what a
    probed scope may declare: writing a worktree-scoped key creates the file,
    and a driver in it must still refuse."""
    server_mod, remote, _seed = fixtures
    async with signed_client(server_mod) as client:
        vault = await _clone(client, remote)
        root = Path(vault["localPath"])
        _git("config", "--local", "extensions.worktreeConfig", "true", cwd=root)
        _git("config", "--worktree", "filter.evil.smudge", "sh -c ':'", cwd=root)
        assert "filter.evil.smudge" in await git_ops.repo_supplied_driver(str(root))


@pytest.mark.asyncio
async def test_probe_refuses_worktree_driver_under_valueless_extension(
    fixtures,
) -> None:
    """git treats a valueless `[extensions] worktreeConfig` as TRUE and honors
    `config.worktree` under it, but a raw `--get` returns an EMPTY string for
    that form — a literal `== "true"` compare reads the extension as off and
    never probes the scope a driver hides in. `--bool` folds every git-true
    spelling to `true`."""
    server_mod, remote, _seed = fixtures
    async with signed_client(server_mod) as client:
        vault = await _clone(client, remote)
        root = Path(vault["localPath"])
        with open(root / ".git" / "config", "a") as fh:
            fh.write("[extensions]\n\tworktreeConfig\n")
        _git("config", "--worktree", "filter.evil.smudge", "sh -c ':'", cwd=root)
        assert "filter.evil.smudge" in await git_ops.repo_supplied_driver(str(root))


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.name == "nt" or sys.platform == "darwin",
    reason="non-UTF-8 bytes are not legal NTFS or APFS/HFS+ name units",
)
async def test_run_git_surrogateescape_round_trips_a_non_utf8_path(tmp_path) -> None:
    """`errors="surrogateescape"` is the decode the gitdir probe passes: a
    path byte that is not valid UTF-8 must survive as a PEP 383 surrogate the
    `os` layer restores byte-exactly, or the empty-scope classifier's lstat
    inspects a U+FFFD path that names nothing and an EXISTING
    `config.worktree` reads as the empty scope (see
    kiro_crew.git_worktree_scope)."""
    repo = tmp_path / os.fsdecode(b"v-\xff")
    repo.mkdir()
    _git("init", "-q", "--template=", ".", cwd=repo)
    code, out, _ = await git_ops.run_git(
        ["rev-parse", "--absolute-git-dir"],
        str(repo),
        check=False,
        errors="surrogateescape",
    )
    assert code == 0
    gitdir = out[:-1] if out.endswith("\n") else out
    assert "\udcff" in gitdir, "the non-UTF-8 byte was rewritten by the decode"
    assert os.path.isdir(gitdir)


@pytest.mark.asyncio
async def test_git_dir_probe_requests_byte_faithful_decoding(monkeypatch, tmp_path) -> None:
    """The canonical-gitdir probe decodes with ``errors="surrogateescape"``: its
    answer is fed to ``os.path.realpath`` and compared against the persisted
    trusted git dir, so a non-UTF-8 path byte has to round-trip through
    ``os.fsencode`` instead of collapsing to a U+FFFD that names no real path."""
    seen: dict[str, Any] = {}

    async def fake_run_git(args, cwd=None, **kwargs):
        seen["args"] = args
        seen["kwargs"] = kwargs
        return 0, f"{tmp_path}\n", ""

    monkeypatch.setattr(git_ops, "run_git", fake_run_git)
    assert await git_ops._git_dir(str(tmp_path)) == os.path.realpath(tmp_path)
    assert seen["args"] == ["rev-parse", "--absolute-git-dir"]
    assert seen["kwargs"]["errors"] == "surrogateescape"


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
            "config",
            "--local",
            "url.https://evil.invalid/.pushInsteadOf",
            "https://github.com/",
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
@pytest.mark.parametrize(
    "key,value",
    [
        ("http.proxy", "http://attacker:8080"),
        ("http.sslVerify", "false"),
        ("http.sslCAInfo", "/tmp/evil-ca.pem"),
    ],
)
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
