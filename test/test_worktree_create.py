"""Tests for POST /api/worktree/create (follow-up card "Start in new worktree").

The endpoint shells out to git, so the tests exercise both halves: input
rejection (branch grammar, non-repo paths, sensitive paths) and the real
happy path against a throwaway git repo in tmp_path.
"""

from __future__ import annotations

import functools
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers import worktree as wt_mod
from kiro_crew.dashboard.handlers.worktree import (
    _FILTER_PROBE_FAILED,
    SandboxUnavailable,
    _checkout_filter,
    _claim_branch,
    _cleanup_partial,
    _dir_slug,
    _match_allowed_root,
    _resolve_base_ref,
    _resolve_commit,
    _run_git,
    _worktree_branches,
    _worktree_config_active,
    api_worktree_create,
)
from kiro_crew.validation import FOLLOWUP_BRANCH_RE, is_valid_followup_branch


def _branch_exists(root: str, branch: str) -> bool:
    """Whether ``refs/heads/<branch>`` resolves in ``root``."""
    return bool(_resolve_commit(root, f"refs/heads/{branch}"))


def _make_app(
    *projects: str, app_claim: str | None = "", user: str = "owner"
) -> web.Application:
    """App whose state exposes one slot per allowed project directory.

    ``app_claim`` mirrors ``token_auth_middleware``'s ``request["app"]``: ``""``
    for a dashboard user, a name for an app caller, ``None`` to leave the key
    absent (auth middleware never ran).
    """

    @web.middleware
    async def claims(request: web.Request, handler):
        if app_claim is not None:
            request["app"] = app_claim
        request["user"] = user
        return await handler(request)

    app = web.Application(middlewares=[claims])
    state = MagicMock()
    # The gate requires the OWNER's own identity, not merely a dashboard claim
    # (GPT review round 12), so the mock must carry a matching owner_id — a bare
    # MagicMock attribute here would 403 every request for the wrong reason.
    state.owner_id = "owner"
    state._slots = {
        f"chat-{i}": MagicMock(project=str(p)) for i, p in enumerate(projects) if p
    }
    app["state"] = state
    app.router.add_post("/api/worktree/create", api_worktree_create)
    return app


# conftest's autouse ``_git_identity`` closes the identity + host-``~/.gitconfig``
# bleeds, but it is FUNCTION-scoped, so the session-scoped template builder below runs
# before it. Pin the same env here so the template is hermetic at either scope.
_GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
}


def _git(*args: str, cwd) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, **_GIT_ENV},
    )


@functools.lru_cache(maxsize=1)
def _sandbox_exec_reason() -> str:
    """"" if a sandboxed git can actually run here, else why it cannot.

    `_run_git` routes through the OS-sandbox chokepoint, and the endpoint refuses
    with a 503 when isolation cannot be established. That is a real platform
    limitation, not a defect, so the tests that exercise git must SKIP there
    rather than fail. A backend-availability probe is not enough: GitHub Actions
    runners pass the user-namespace probe but deny `unshare(NEWNS)` at exec time
    (errno 1), which the launcher can only report from the child.
    """
    with tempfile.TemporaryDirectory() as tmp:
        try:
            proc = _run_git(["--version"], tmp)
        except SandboxUnavailable as exc:
            return str(exc) or "sandbox unavailable"
        except OSError as exc:  # pragma: no cover - no git binary at all
            return f"git unavailable: {exc}"
    return "" if proc.returncode == 0 else (proc.stderr or "git failed").strip()


def _require_sandbox_exec() -> None:
    reason = _sandbox_exec_reason()
    if reason:
        pytest.skip(f"sandboxed git cannot run on this host: {reason[:120]}")


def _passthrough_spawn(argv, mode="standard", **kw):
    """Stand in for ``sandboxed_spawn_argv`` so ``_run_git``'s own result handling
    can be asserted on ANY host.

    The real wrapper raises when the host has no sandbox backend (Windows
    runners, macOS without ``sandbox-exec``), which turns a "given this git
    result…" test into a RuntimeError about isolation before the result is ever
    examined. Skipping would lose the coverage, and one such test was passing on
    Windows for the wrong reason — that RuntimeError is surfaced as
    ``SandboxUnavailable`` too, so the assertion held without the code under test
    running (GPT review round 12 / round-11 CI).
    """
    return list(argv), {}, None


@pytest.fixture(scope="session")
def _repo_template(tmp_path_factory):
    """Build the one-commit git repo once per session; `repo` copies it per test.

    The five git subprocesses below cost ~1s of setup on each of the 81 tests here,
    which made this the heaviest file on the slowest CI shard.

    Session scope is safe because the template is never handed to a test, only copied
    from, so a test that adds a worktree, branch or commit cannot reach another's.
    """
    template = tmp_path_factory.mktemp("worktree-seed") / "proj"
    template.mkdir()
    _git("init", "-q", "-b", "main", cwd=template)
    _git("config", "user.email", "test@example.com", cwd=template)
    _git("config", "user.name", "Test", cwd=template)
    (template / "README.md").write_text("hi\n")
    _git("add", "README.md", cwd=template)
    _git("commit", "-q", "-m", "init", cwd=template)
    return template


@pytest.fixture
def repo(tmp_path, request):
    """A minimal git repo with one commit on branch `main`.

    Requests :func:`sandbox_can_exec` so every git-touching test skips (rather
    than fails) on a host where isolation cannot be established — see that
    fixture for why the backend probe is not sufficient.

    The template is resolved through ``request.getfixturevalue`` AFTER that skip
    check, not as a parameter: pytest sets a declared fixture up before the body
    runs, which would build the template on a host that then skips every test using
    it -- turning a clean skip into five pointless git spawns (or a hard error where
    git is unusable).
    """
    _require_sandbox_exec()
    root = tmp_path / "proj"
    shutil.copytree(request.getfixturevalue("_repo_template"), root)
    return root


class TestDirSlug:
    def test_uses_last_path_segment(self):
        assert _dir_slug("feat/upload-limit") == "upload-limit"

    def test_strips_unsafe_chars(self):
        assert "/" not in _dir_slug("feat/a_b.c-d")

    def test_falls_back_when_slug_empty(self):
        assert _dir_slug("feat/...") == "followup"

    def test_bounds_length(self):
        assert len(_dir_slug("feat/" + "a" * 200)) <= 60


class TestWorktreeCreate:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "branch",
        [
            "--force",
            "feat/../escape",
            "feat/with space",
            "feat/semi;colon",
            "feat/tilde~1",
            "-b",
            "",
        ],
    )
    async def test_rejects_unsafe_branch(self, repo, branch):
        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.post(
                "/api/worktree/create", json={"repo": str(repo), "branch": branch}
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_rejects_non_string_inputs(self, repo):
        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.post("/api/worktree/create", json={"repo": 1, "branch": "feat/x"})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_rejects_missing_directory(self, tmp_path):
        # Reaches the git probe, so it needs a host where the sandbox can run
        # (a refusal answers 503 before any directory check is reported).
        _require_sandbox_exec()
        async with TestClient(TestServer(_make_app(str(tmp_path)))) as client:
            resp = await client.post(
                "/api/worktree/create",
                json={"repo": str(tmp_path / "nope"), "branch": "feat/x"},
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_rejects_non_git_directory(self, tmp_path):
        _require_sandbox_exec()
        plain = tmp_path / "plain"
        plain.mkdir()
        async with TestClient(TestServer(_make_app(str(plain)))) as client:
            resp = await client.post(
                "/api/worktree/create", json={"repo": str(plain), "branch": "feat/x"}
            )
            assert resp.status == 400
            assert "git repository" in (await resp.json())["error"]

    @pytest.mark.asyncio
    async def test_rejects_invalid_json(self):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/worktree/create",
                data="nope",
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_creates_sibling_worktree(self, repo):
        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.post(
                "/api/worktree/create",
                json={"repo": str(repo), "branch": "feat/upload-limit"},
            )
            assert resp.status == 200, await resp.text()
            data = await resp.json()
        created = data["path"]
        assert created.endswith("proj-wt-upload-limit")
        # Sibling of the repo, not nested inside it.
        assert created == str(repo.parent / "proj-wt-upload-limit")
        assert (repo.parent / "proj-wt-upload-limit" / "README.md").is_file()
        assert data["branch"] == "feat/upload-limit"

    @pytest.mark.asyncio
    async def test_path_inside_repo_resolves_to_toplevel(self, repo):
        """A path deeper in the tree must not make git operate on a subdirectory."""
        nested = repo / "src" / "deep"
        nested.mkdir(parents=True)
        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.post(
                "/api/worktree/create", json={"repo": str(nested), "branch": "feat/deep"}
            )
            assert resp.status == 200, await resp.text()
            data = await resp.json()
        assert data["path"] == str(repo.parent / "proj-wt-deep")

    @pytest.mark.asyncio
    async def test_existing_branch_returns_409(self, repo):
        _git("branch", "feat/taken", cwd=repo)
        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.post(
                "/api/worktree/create", json={"repo": str(repo), "branch": "feat/taken"}
            )
            assert resp.status == 409

    @pytest.mark.asyncio
    async def test_existing_directory_returns_409(self, repo):
        (repo.parent / "proj-wt-clash").mkdir()
        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.post(
                "/api/worktree/create", json={"repo": str(repo), "branch": "feat/clash"}
            )
            assert resp.status == 409


class TestNoRepositoryCodeExecution:
    """`git worktree add` must not run the repository's own post-checkout hook.

    This is the regression guard for the GPT HIGH on PR #461: the hook is
    repo-controlled code, and it executing is what would otherwise demand
    OS-sandbox isolation on this spawn. The control case asserts the hook WOULD
    have fired without the overrides, so the test cannot silently pass because
    the harness failed to install a working hook.
    """

    @staticmethod
    def _install_hook(repo, marker):
        hooks = repo / ".git" / "hooks"
        hooks.mkdir(parents=True, exist_ok=True)
        hook = hooks / "post-checkout"
        hook.write_text(f'#!/bin/sh\ntouch "{marker}"\n')
        hook.chmod(0o755)

    @pytest.mark.skipif(os.name != "posix", reason="shell hook script needs POSIX sh")
    def test_control_hook_fires_without_overrides(self, repo, tmp_path):
        marker = tmp_path / "control-marker"
        self._install_hook(repo, marker)
        subprocess.run(
            ["git", "worktree", "add", str(tmp_path / "wt-control"), "-b", "feat/c", "HEAD"],
            cwd=str(repo),
            capture_output=True,
            check=True,
        )
        assert marker.exists(), "harness is broken: the hook never fired even unguarded"

    @pytest.mark.skipif(os.name != "posix", reason="shell hook script needs POSIX sh")
    @pytest.mark.asyncio
    async def test_endpoint_does_not_run_repo_hook(self, repo, tmp_path):
        marker = tmp_path / "endpoint-marker"
        self._install_hook(repo, marker)
        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.post(
                "/api/worktree/create", json={"repo": str(repo), "branch": "feat/guarded"}
            )
            assert resp.status == 200, await resp.text()
            created = (await resp.json())["path"]
        assert not marker.exists(), "repository post-checkout hook executed"
        # The suppression must not break the checkout itself.
        assert (pathlib.Path(created) / "README.md").is_file()

    @pytest.mark.skipif(os.name != "posix", reason="shell hook script needs POSIX sh")
    @pytest.mark.asyncio
    async def test_hooks_path_is_not_a_repo_writable_location(self, repo, tmp_path):
        """A hook planted at the OLD in-repo sentinel path must not execute.

        Round 5 of the PR #461 review: `core.hooksPath` resolves relative to the
        repository, so pointing it at `.git/kirocrew-no-hooks` left the
        suppression target inside a directory the checkout's own preparer can
        write. Planting `post-checkout` there turned the guard into the execution
        vector. The sink is now `os.devnull`, which is not a directory at all.
        """
        marker = tmp_path / "sentinel-marker"
        planted = repo / ".git" / "kirocrew-no-hooks"
        planted.mkdir(parents=True, exist_ok=True)
        hook = planted / "post-checkout"
        hook.write_text(f'#!/bin/sh\ntouch "{marker}"\n')
        hook.chmod(0o755)
        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.post(
                "/api/worktree/create", json={"repo": str(repo), "branch": "feat/sentinel"}
            )
            assert resp.status == 200, await resp.text()
        assert not marker.exists(), "hook planted at the in-repo sentinel path executed"

    def test_hooks_sink_is_a_non_directory_device(self):
        """Round 8 HIGH: a same-uid gateway-owned directory was still plantable.

        `os.devnull` cannot be replaced or filled, so there is no window between
        one git call and the next in which a hook could appear.
        """
        from kiro_crew.dashboard.handlers import worktree as wt

        assert wt._HOOKS_SINK == os.devnull
        assert not os.path.isdir(wt._HOOKS_SINK)
        argv = wt._git_no_repo_code()
        assert f"core.hooksPath={os.devnull}" in argv
        assert "core.fsmonitor=false" in argv


class TestIdempotentReentry:
    """A retry after the caller's second step failed must complete, not 409.

    The card creates the worktree, then opens a session. If the session step
    fails the worktree is already on disk, so a naive retry dead-ends on both
    "directory already exists" and "branch already exists" (GPT review, PR #461).
    """

    @pytest.mark.asyncio
    async def test_retry_reuses_our_own_worktree(self, repo):
        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            first = await client.post(
                "/api/worktree/create", json={"repo": str(repo), "branch": "feat/retry"}
            )
            assert first.status == 200, await first.text()
            first_body = await first.json()
            assert first_body["reused"] is False

            second = await client.post(
                "/api/worktree/create", json={"repo": str(repo), "branch": "feat/retry"}
            )
            assert second.status == 200, await second.text()
            second_body = await second.json()
        assert second_body["reused"] is True
        assert second_body["path"] == first_body["path"]

    @pytest.mark.asyncio
    async def test_unrelated_directory_at_dest_still_409s(self, repo):
        """Idempotency must not adopt a directory that is not our worktree."""
        squatter = repo.parent / "proj-wt-squat"
        squatter.mkdir()
        (squatter / "someone-elses-file").write_text("x")
        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.post(
                "/api/worktree/create", json={"repo": str(repo), "branch": "feat/squat"}
            )
            assert resp.status == 409
        # Untouched.
        assert (squatter / "someone-elses-file").is_file()

    @pytest.mark.asyncio
    async def test_failed_add_leaves_no_branch_or_directory(self, repo):
        """A failing `worktree add` must not leave artifacts that block a retry.

        The branch is claimed before the add now, so the cleanup path has real
        work to do: fail only the `worktree add` invocation and pass every other
        git call through.
        """
        real_run_git = _run_git

        def fail_add(args, cwd):
            if args[:2] == ["worktree", "add"]:
                return subprocess.CompletedProcess(args, 1, "", "fatal: injected failure\n")
            return real_run_git(args, cwd)

        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            with patch(
                "kiro_crew.dashboard.handlers.worktree._run_git", side_effect=fail_add
            ):
                resp = await client.post(
                    "/api/worktree/create", json={"repo": str(repo), "branch": "feat/doomed"}
                )
                assert resp.status == 400
            assert not (repo.parent / "proj-wt-doomed").exists()
            assert not _branch_exists(str(repo), "feat/doomed")
            # And the retry path is clear: a good request now succeeds.
            ok = await client.post(
                "/api/worktree/create", json={"repo": str(repo), "branch": "feat/doomed"}
            )
            assert ok.status == 200, await ok.text()

    @pytest.mark.asyncio
    async def test_unresolvable_base_creates_nothing(self, repo):
        """A base ref that resolves to no commit fails before anything is made."""
        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            with patch(
                "kiro_crew.dashboard.handlers.worktree._resolve_base_ref",
                return_value="refs/heads/does-not-exist-xyz",
            ):
                resp = await client.post(
                    "/api/worktree/create", json={"repo": str(repo), "branch": "feat/nobase"}
                )
                assert resp.status == 400
        assert not (repo.parent / "proj-wt-nobase").exists()
        assert not _branch_exists(str(repo), "feat/nobase")


class TestConcurrencySafety:
    """GPT review round 3, HIGH: check-then-create let two same-branch requests
    both proceed, and the loser's cleanup then destroyed the winner's worktree.
    """

    def test_claim_is_atomic(self, repo):
        """The second claim of the same ref must lose, not overwrite."""
        sha = _resolve_commit(str(repo), "HEAD")
        assert _claim_branch(str(repo), "feat/claim", sha) is True
        assert _claim_branch(str(repo), "feat/claim", sha) is False

    @pytest.mark.asyncio
    async def test_second_request_for_same_branch_409s_and_spares_the_first(self, repo):
        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            first = await client.post(
                "/api/worktree/create", json={"repo": str(repo), "branch": "feat/race"}
            )
            assert first.status == 200, await first.text()
            path = (await first.json())["path"]
            # Same branch, but the destination is gone (user moved it): the claim
            # must still refuse rather than re-create and then clean up.
            os.rename(path, path + "-moved")
            second = await client.post(
                "/api/worktree/create", json={"repo": str(repo), "branch": "feat/race"}
            )
            assert second.status == 409
        assert _branch_exists(str(repo), "feat/race")
        assert os.path.isdir(path + "-moved")

    def test_cleanup_spares_a_worktree_registered_to_another_branch(self, repo):
        """Cleanup removes only what the request can prove it created."""
        theirs = str(repo.parent / "proj-wt-theirs")
        sha = _resolve_commit(str(repo), "HEAD")
        assert _claim_branch(str(repo), "feat/theirs", sha) is True
        assert _run_git(["worktree", "add", theirs, "feat/theirs"], str(repo)).returncode == 0
        # A different request unwinds ITS branch, whose derived dest collides.
        assert _claim_branch(str(repo), "feat/ours", sha) is True
        _cleanup_partial(
            str(repo), theirs, "feat/ours", claimed=True, created=True, base_sha=sha
        )
        assert os.path.isdir(theirs), "another branch's worktree was destroyed"
        assert _branch_exists(str(repo), "feat/theirs")
        assert not _branch_exists(str(repo), "feat/ours")

    def test_cleanup_never_touches_a_directory_it_did_not_create(self, repo):
        """GPT round 4 HIGH: `created=False` must mean hands off the path.

        Previously "git lists nothing here" authorized an `rmtree`, which is also
        what a transient listing failure looks like.
        """
        squatter = repo.parent / "proj-wt-untouched"
        squatter.mkdir()
        (squatter / "precious.txt").write_text("do not delete")
        _cleanup_partial(
            str(repo), str(squatter), "feat/whatever", claimed=False, created=False
        )
        assert (squatter / "precious.txt").is_file()

    def test_cleanup_survives_a_failed_worktree_listing(self, repo):
        """A listing failure must not be read as "nothing is registered"."""
        ours = repo.parent / "proj-wt-ours"
        ours.mkdir()
        (ours / "marker").write_text("x")
        with patch(
            "kiro_crew.dashboard.handlers.worktree._worktree_branches", return_value=None
        ):
            # created=True: the mkdir claim is what authorizes removal, not the
            # (unavailable) listing.
            _cleanup_partial(str(repo), str(ours), "feat/ours", claimed=False, created=True)
        assert not ours.exists()

    def test_cleanup_deletes_the_branch_after_an_rmtree_fallback(self, repo):
        """Round 7 MEDIUM: prune must precede `branch -D`.

        When `worktree remove` fails and the directory is dropped with `rmtree`,
        git still lists the worktree as checked out on that branch and refuses
        `branch -D` ("used by worktree"). Pruning afterwards left the claimed
        branch behind, so the retry the endpoint promises hit "branch already
        exists" instead of reusing the worktree.
        """
        dest = str(repo.parent / "proj-wt-stale")
        sha = _resolve_commit(str(repo), "HEAD")
        assert _claim_branch(str(repo), "feat/stale", sha) is True
        assert _run_git(["worktree", "add", dest, "feat/stale"], str(repo)).returncode == 0
        real_run_git = _run_git

        def _fail_worktree_remove(args, cwd):
            if args[:2] == ["worktree", "remove"]:
                return subprocess.CompletedProcess(args, 1, "", "fatal: forced failure")
            return real_run_git(args, cwd)

        with patch(
            "kiro_crew.dashboard.handlers.worktree._run_git", side_effect=_fail_worktree_remove
        ):
            _cleanup_partial(
                str(repo), dest, "feat/stale", claimed=True, created=True, base_sha=sha
            )
        assert not os.path.isdir(dest)
        assert not _branch_exists(str(repo), "feat/stale"), "claimed branch survived cleanup"

    def test_cleanup_spares_a_branch_another_worktree_adopted(self, repo):
        """Round 13 BLOCKING: `update-ref -d` has no "used by worktree" guard.

        A concurrent `git worktree add` can check out the branch this request
        claimed while the request is failing. Compare-and-delete still matched
        (the ref had not moved), so cleanup deleted it out from under the other
        worktree, leaving it on a dangling ref.
        """
        sha = _resolve_commit(str(repo), "HEAD")
        assert _claim_branch(str(repo), "feat/adopted", sha) is True
        # Somebody else checks the claimed branch out before our cleanup runs.
        theirs = str(repo.parent / "proj-wt-adopted")
        assert _run_git(["worktree", "add", theirs, "feat/adopted"], str(repo)).returncode == 0
        ours = str(repo.parent / "proj-wt-ours-failed")
        _cleanup_partial(
            str(repo), ours, "feat/adopted", claimed=True, created=False, base_sha=sha
        )
        assert _branch_exists(str(repo), "feat/adopted"), "deleted a branch in use"
        head = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], theirs)
        assert head.stdout.strip() == "feat/adopted"

    def test_cleanup_keeps_the_branch_when_the_listing_is_unreadable(self, repo):
        """Adoption cannot be ruled out from an unreadable listing, and a retry
        reporting "already exists" is recoverable where a broken worktree is not."""
        sha = _resolve_commit(str(repo), "HEAD")
        assert _claim_branch(str(repo), "feat/unknown", sha) is True
        with patch(
            "kiro_crew.dashboard.handlers.worktree._worktree_branches", return_value=None
        ):
            _cleanup_partial(
                str(repo),
                str(repo.parent / "proj-wt-unknown"),
                "feat/unknown",
                claimed=True,
                created=False,
                base_sha=sha,
            )
        assert _branch_exists(str(repo), "feat/unknown")

    def test_cleanup_spares_a_branch_that_advanced_after_the_claim(self, repo, tmp_path):
        """Round 10 HIGH: `branch -D` force-deletes, so a concurrent commit landing
        on the claimed ref between the claim and the cleanup was discarded with it.

        Compare-and-delete (`update-ref -d <ref> <old>`) refuses once the ref has
        moved, so those commits stay reachable.
        """
        sha = _resolve_commit(str(repo), "HEAD")
        assert _claim_branch(str(repo), "feat/advanced", sha) is True
        # A concurrent process advances the claimed ref (a commit pushed into it).
        wt = tmp_path / "concurrent"
        assert _run_git(
            ["worktree", "add", str(wt), "feat/advanced"], str(repo)
        ).returncode == 0
        (wt / "new.txt").write_text("work someone else did\n")
        _git("add", "new.txt", cwd=wt)
        _git("-c", "user.email=a@b.c", "-c", "user.name=a", "commit", "-qm", "concurrent", cwd=wt)
        advanced = _resolve_commit(str(repo), "refs/heads/feat/advanced")
        assert advanced and advanced != sha
        # Our create fails and unwinds, still believing the ref is at `sha`.
        _cleanup_partial(
            str(repo),
            str(repo.parent / "proj-wt-advanced"),
            "feat/advanced",
            claimed=True,
            created=False,
            base_sha=sha,
        )
        assert _resolve_commit(str(repo), "refs/heads/feat/advanced") == advanced, (
            "cleanup discarded commits added after the claim"
        )

    def test_cleanup_leaves_a_branch_it_did_not_claim(self, repo):
        sha = _resolve_commit(str(repo), "HEAD")
        assert _claim_branch(str(repo), "feat/preexisting", sha) is True
        _cleanup_partial(
            str(repo),
            str(repo.parent / "proj-wt-preexisting"),
            "feat/preexisting",
            claimed=False,
            created=False,
        )
        assert _branch_exists(str(repo), "feat/preexisting")

    @pytest.mark.asyncio
    async def test_unreadable_worktree_list_is_a_503_not_a_create(self, repo):
        """If git cannot enumerate worktrees, refuse rather than guess."""
        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            with patch(
                "kiro_crew.dashboard.handlers.worktree._worktree_branches", return_value=None
            ):
                resp = await client.post(
                    "/api/worktree/create", json={"repo": str(repo), "branch": "feat/blind"}
                )
                assert resp.status == 503, await resp.text()
        assert not (repo.parent / "proj-wt-blind").exists()
        assert not _branch_exists(str(repo), "feat/blind")


class TestSlugCollision:
    """GPT review round 3, MEDIUM: `_dir_slug` keeps only a branch's last
    segment, so `feat/foo` and `fix/foo` derive the same destination. Reuse keyed
    on the path alone handed back the wrong branch's worktree.
    """

    @pytest.mark.asyncio
    async def test_same_slug_different_branch_is_not_reused(self, repo):
        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            first = await client.post(
                "/api/worktree/create", json={"repo": str(repo), "branch": "feat/shared"}
            )
            assert first.status == 200, await first.text()
            dest = (await first.json())["path"]
            second = await client.post(
                "/api/worktree/create", json={"repo": str(repo), "branch": "fix/shared"}
            )
            assert second.status == 409, await second.text()
            body = await second.json()
        assert "already exists" in body["error"]
        # The first worktree is untouched and still on its own branch.
        assert _worktree_branches(str(repo))[os.path.normcase(dest)] == "feat/shared"
        assert not _branch_exists(str(repo), "fix/shared")


class TestCheckoutFilters:
    """GPT review round 3, HIGH: `.gitattributes` can name a content filter whose
    driver is defined in repo-local config, and checkout runs it. `-c` cannot
    disable an arbitrary filter name, so such a repo is refused.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("key", ["filter.evil.process", "filter.evil.smudge"])
    async def test_local_filter_config_is_refused(self, repo, key):
        _git("config", "--local", key, "sh -c 'touch /tmp/pwned'", cwd=repo)
        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.post(
                "/api/worktree/create", json={"repo": str(repo), "branch": "feat/filtered"}
            )
            assert resp.status == 409, await resp.text()
            body = await resp.json()
        assert "content filter" in body["error"]
        assert not (repo.parent / "proj-wt-filtered").exists()
        assert not _branch_exists(str(repo), "feat/filtered")

    @pytest.mark.asyncio
    async def test_unrelated_local_config_is_not_refused(self, repo):
        """Only filter drivers gate the operation, not config in general."""
        _git("config", "--local", "filter.evil.required", "true", cwd=repo)
        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.post(
                "/api/worktree/create", json={"repo": str(repo), "branch": "feat/allowed"}
            )
            assert resp.status == 200, await resp.text()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("key", ["filter.evil.process", "filter.evil.smudge"])
    async def test_worktree_scoped_filter_config_is_refused(self, repo, key):
        """Round 6 HIGH: `--local` does not report worktree-scoped keys.

        With `extensions.worktreeConfig=true` git also reads
        `$GIT_COMMON_DIR/config.worktree`. A filter driver declared only there was
        invisible to the old `--local`-only probe, and `git worktree add` executed
        it during checkout (verified empirically before this fix).
        """
        _git("config", "extensions.worktreeConfig", "true", cwd=repo)
        _git("config", "--worktree", key, "sh -c 'touch /tmp/pwned'", cwd=repo)
        # Precondition: the old probe genuinely could not see this key.
        local = _run_git(["config", "--local", "--name-only", "--list"], str(repo))
        assert key not in local.stdout.splitlines()
        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.post(
                "/api/worktree/create", json={"repo": str(repo), "branch": "feat/wtfiltered"}
            )
            assert resp.status == 409, await resp.text()
            body = await resp.json()
        assert "content filter" in body["error"]
        assert not (repo.parent / "proj-wt-wtfiltered").exists()
        assert not _branch_exists(str(repo), "feat/wtfiltered")

    @pytest.mark.asyncio
    async def test_linked_worktree_scoped_filter_config_is_refused(self, repo, tmp_path):
        """Round 7 HIGH: for a LINKED worktree, `config.worktree` lives under
        `$GIT_DIR` (`<common>/worktrees/<id>`), not under the common dir.

        Probing the common dir therefore missed a filter declared in a linked
        worktree's own config — `_worktree_config_active` returned False, the
        `--worktree` scope was skipped, and the driver executed during checkout
        (verified empirically before this fix).
        """
        _git("config", "extensions.worktreeConfig", "true", cwd=repo)
        linked = tmp_path / "linked"
        _git("worktree", "add", str(linked), "-b", "linked-br", "HEAD", cwd=repo)
        _git("config", "--worktree", "filter.evil.smudge", "sh -c 'touch /tmp/pwned'", cwd=linked)
        # Precondition: the file is NOT where the common-dir probe looked.
        common = _run_git(["rev-parse", "--git-common-dir"], str(linked)).stdout.strip()
        gitdir = _run_git(["rev-parse", "--absolute-git-dir"], str(linked)).stdout.strip()
        assert not os.path.isfile(os.path.join(common, "config.worktree"))
        assert os.path.isfile(os.path.join(gitdir, "config.worktree"))
        assert _worktree_config_active(str(linked))
        async with TestClient(TestServer(_make_app(str(linked)))) as client:
            resp = await client.post(
                "/api/worktree/create", json={"repo": str(linked), "branch": "feat/linked"}
            )
            assert resp.status == 409, await resp.text()
            assert "content filter" in (await resp.json())["error"]
        assert not _branch_exists(str(repo), "feat/linked")

    @pytest.mark.asyncio
    async def test_worktree_config_enabled_but_empty_still_succeeds(self, repo):
        """The extension alone must not refuse: `--worktree --list` exits 128 when
        no `config.worktree` file exists, and that is not a filter."""
        _git("config", "extensions.worktreeConfig", "true", cwd=repo)
        assert not _worktree_config_active(str(repo))
        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.post(
                "/api/worktree/create", json={"repo": str(repo), "branch": "feat/extonly"}
            )
            assert resp.status == 200, await resp.text()

    def test_probe_failure_fails_closed(self, repo):
        """An unreadable config scope refuses rather than assuming "no filter"."""
        failed = subprocess.CompletedProcess(args=["git"], returncode=128, stdout="", stderr="x")
        with patch(
            "kiro_crew.dashboard.handlers.worktree._run_git", return_value=failed
        ):
            assert _checkout_filter(str(repo)) == _FILTER_PROBE_FAILED

    @pytest.mark.asyncio
    async def test_included_filter_config_is_refused(self, repo, tmp_path):
        """Round 8 HIGH: `include.path` hid the driver from the probe.

        For a SPECIFIC scope query (`--local`/`--worktree`) git defaults
        include-following OFF, so a `filter.*.smudge` reached through
        `include.path` was invisible to the probe while still resolving — and
        executing — during checkout (verified empirically before this fix).
        """
        included = tmp_path / "inc.cfg"
        included.write_text('[filter "evil"]\n\tsmudge = "sh -c \\"touch /tmp/pwned\\""\n')
        _git("config", "--local", "include.path", str(included), cwd=repo)
        # Preconditions: resolvable by git, invisible without --includes.
        resolved = _run_git(["config", "--includes", "--get", "filter.evil.smudge"], str(repo))
        assert resolved.stdout.strip()
        blind = _run_git(["config", "--local", "--name-only", "--list"], str(repo))
        assert not [k for k in blind.stdout.splitlines() if k.startswith("filter.")]
        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.post(
                "/api/worktree/create", json={"repo": str(repo), "branch": "feat/included"}
            )
            assert resp.status == 409, await resp.text()
            assert "content filter" in (await resp.json())["error"]
        assert not _branch_exists(str(repo), "feat/included")


class TestRound9Hardening:
    """Regressions for the round-9 review findings."""

    @pytest.mark.parametrize(
        "bad",
        [
            "foo..bar",
            "foo.",
            "foo.lock",
            "HEAD",
            "feat/x.lock",
            # Windows reserved device stems: a loose ref is a FILE, and these
            # cannot be created on Windows (with or without an extension).
            "CON",
            "con",
            "feat/AUX",
            "NUL",
            "COM1",
            "lpt9",
            "feat/con.txt",
        ],
    )
    def test_git_invalid_refs_are_rejected_up_front(self, bad):
        """The character grammar accepted refs git itself refuses, and the failure
        then surfaced as a misleading "Branch already exists"."""
        assert FOLLOWUP_BRANCH_RE.match(bad), "precondition: the regex alone allows it"
        assert not is_valid_followup_branch(bad)

    @pytest.mark.parametrize("good", ["feat/upload-limit", "followup/add-rate-limits", "x.y"])
    def test_ordinary_branches_still_pass(self, good):
        assert is_valid_followup_branch(good)

    @pytest.mark.asyncio
    async def test_invalid_ref_is_a_400_not_a_409(self, repo):
        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.post(
                "/api/worktree/create", json={"repo": str(repo), "branch": "feat/x.lock"}
            )
            assert resp.status == 400, await resp.text()

    @pytest.mark.asyncio
    async def test_sandbox_unavailable_refuses_with_503(self, repo):
        """Fail CLOSED: no OS isolation available means the git spawn does not run."""
        from kiro_crew.dashboard.handlers import worktree as wt

        with patch.object(
            wt, "sandboxed_spawn_argv", side_effect=RuntimeError("no backend")
        ):
            async with TestClient(TestServer(_make_app(str(repo)))) as client:
                resp = await client.post(
                    "/api/worktree/create", json={"repo": str(repo), "branch": "feat/nosbx"}
                )
                assert resp.status == 503, await resp.text()
                assert "sandbox" in (await resp.json())["error"].lower()
        assert not _branch_exists(str(repo), "feat/nosbx")

    def test_git_runs_in_strict_sandbox_mode(self):
        """Round 11 BLOCKING: `--includes` means `include.path` is repo-controlled,
        so a hostile checkout could point it at `~/.aws/credentials` and have git
        read that file as config. "standard" leaves those paths visible; strict
        bind-mounts them away. Pinned so the mode cannot silently widen.
        """
        from kiro_crew.dashboard.handlers import worktree as wt

        assert wt._SANDBOX_MODE == "strict"
        seen: dict = {}

        def fake_spawn(argv, mode="standard", **kw):
            seen["mode"] = mode
            return list(argv), {}, None

        ok = subprocess.CompletedProcess(args=["git"], returncode=0, stdout="", stderr="")
        with patch.object(wt, "sandboxed_spawn_argv", side_effect=fake_spawn), patch.object(
            wt.subprocess, "run", return_value=ok
        ):
            wt._run_git(["--version"], os.getcwd())
        assert seen["mode"] == "strict"

    def test_launcher_isolation_failure_is_a_refusal_not_a_git_error(self):
        """A sandbox that cannot establish isolation IN THE CHILD must not read as
        a git error.

        `wrap_argv`'s backend probe passes on GitHub Actions runners, but
        `unshare(NEWNS)` is denied at exec time (errno 1). git never runs, and the
        non-zero exit was being reported downstream as "Not a git repository" —
        a misdiagnosis that sent the user looking at their repo instead of the
        host. Round 9's CI run is where this surfaced.
        """
        from kiro_crew.dashboard.handlers import worktree as wt

        denied = subprocess.CompletedProcess(
            args=["git"],
            returncode=1,
            stdout="",
            stderr="sandbox: unshare(NEWNS) failed: errno 1\n",
        )
        with patch.object(
            wt, "sandboxed_spawn_argv", side_effect=_passthrough_spawn
        ), patch.object(wt.subprocess, "run", return_value=denied):
            with pytest.raises(SandboxUnavailable):
                wt._run_git(["--version"], os.getcwd())

    def test_a_real_git_failure_is_still_a_git_failure(self):
        """Only the launcher's own `sandbox: ` prefix means "no isolation"; an
        ordinary non-zero git exit must pass through untouched."""
        from kiro_crew.dashboard.handlers import worktree as wt

        failed = subprocess.CompletedProcess(
            args=["git"], returncode=128, stdout="", stderr="fatal: not a git repository\n"
        )
        with patch.object(
            wt, "sandboxed_spawn_argv", side_effect=_passthrough_spawn
        ), patch.object(wt.subprocess, "run", return_value=failed):
            proc = wt._run_git(["status"], os.getcwd())
        assert proc.returncode == 128

    def test_worktree_listing_survives_a_newline_in_a_path(self, repo, tmp_path):
        """`--porcelain` alone splits such a path across records, so the entry never
        matches and a retry 409s instead of reporting `reused`."""
        if os.name != "posix":
            pytest.skip("NTFS rejects newlines in path components")
        dest = tmp_path / "wt\nnewline"
        assert _run_git(
            ["worktree", "add", str(dest), "-b", "feat/nl", "HEAD"], str(repo)
        ).returncode == 0
        trees = _worktree_branches(str(repo))
        assert trees is not None
        assert trees.get(os.path.normcase(os.path.realpath(str(dest)))) == "feat/nl"


class TestCallerIsolation:
    """Round 8 HIGH: the allow-list spans EVERY slot's project, so an app caller
    reaching this endpoint could create a worktree in another app's repository."""

    @pytest.mark.asyncio
    async def test_app_caller_is_refused(self, repo):
        app = _make_app(str(repo), app_claim="some-app", user="some-app")
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/worktree/create", json={"repo": str(repo), "branch": "feat/app"}
            )
            assert resp.status == 403
        assert not (repo.parent / "proj-wt-app").exists()
        assert not _branch_exists(str(repo), "feat/app")

    @pytest.mark.asyncio
    async def test_non_owner_dashboard_subject_is_refused(self, repo):
        """Round 12 BLOCKING: a dashboard token minted for another subject carries
        `app == ""` and passed the round-8 gate, so it could create a worktree —
        and a branch — in the OWNER's repository."""
        app = _make_app(str(repo), app_claim="", user="somebody-else")
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/worktree/create", json={"repo": str(repo), "branch": "feat/nonowner"}
            )
            assert resp.status == 403
        assert not _branch_exists(str(repo), "feat/nonowner")

    @pytest.mark.asyncio
    async def test_absent_auth_claim_is_refused(self, repo):
        """An absent claim means the auth middleware never ran — fail closed."""
        app = _make_app(str(repo), app_claim=None)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/worktree/create", json={"repo": str(repo), "branch": "feat/noauth"}
            )
            assert resp.status == 403
        assert not _branch_exists(str(repo), "feat/noauth")


class TestResolveBaseRef:
    def test_falls_back_to_head_without_remote(self, repo):
        assert _resolve_base_ref(str(repo)) == "HEAD"


class TestAllowedRootBoundary:
    """Unit coverage for the path barrier that answers the CodeQL path finding.

    Paths are built with ``os.path.join`` rather than hardcoded POSIX literals:
    the matcher is ``os.sep``-based, so a ``/``-separated literal fails on
    Windows for reasons that have nothing to do with the logic under test.
    """

    @staticmethod
    def _p(*parts: str) -> str:
        return os.path.normpath(os.path.join(os.sep + "srv", *parts))

    def test_exact_root_returns_that_root(self):
        root = self._p("repo")
        assert _match_allowed_root(root, [root]) == root

    def test_descendant_returns_the_root_not_the_candidate(self):
        root = self._p("repo")
        assert _match_allowed_root(self._p("repo", "src", "deep"), [root]) == root

    def test_sibling_prefix_not_allowed(self):
        # "…/repo-evil" shares a string prefix with "…/repo" but is a different
        # directory; a naive startswith would let it through.
        assert _match_allowed_root(self._p("repo-evil"), [self._p("repo")]) is None

    def test_ancestor_not_allowed(self):
        assert _match_allowed_root(os.sep + "srv", [self._p("repo")]) is None

    def test_empty_root_list_denies_everything(self):
        assert _match_allowed_root(self._p("repo"), []) is None

    def test_first_matching_root_wins(self):
        a, b = self._p("a"), self._p("b")
        assert _match_allowed_root(self._p("b", "sub"), [a, b]) == b


class TestRepoAllowList:
    @pytest.mark.asyncio
    async def test_repo_outside_slot_projects_is_denied(self, repo):
        """A real git repo the caller was never granted must not be usable."""
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.post(
                "/api/worktree/create", json={"repo": str(repo), "branch": "feat/x"}
            )
            assert resp.status == 403
        assert not (repo.parent / "proj-wt-x").exists()

    @pytest.mark.asyncio
    async def test_toplevel_above_every_allowed_root_is_denied(self, repo):
        """Resolving upward out of an allowed subdirectory is refused.

        Only ``<repo>/src`` is granted, but git's toplevel for it is ``<repo>``
        — an ancestor of the grant. The second barrier catches that.
        """
        nested = repo / "src"
        nested.mkdir()
        async with TestClient(TestServer(_make_app(str(nested)))) as client:
            resp = await client.post(
                "/api/worktree/create", json={"repo": str(nested), "branch": "feat/x"}
            )
            assert resp.status == 403
            assert "outside" in (await resp.json())["error"]
        assert not (repo.parent / "proj-wt-x").exists()


class TestLauncherAdvisoryIsNotARefusal:
    """A sandbox launcher WARNING must never be read as "no sandbox backend".

    The launcher's pre-exec hardlink scan degrades OPEN when it exhausts its
    per-root file budget: /tmp on a busy host passes that budget from ordinary
    churn, and failing closed would break every sandboxed spawn on such a host. It
    says so on the child's stderr and execs the child anyway.

    Reading that advisory as a refusal broke the endpoint on exactly those hosts,
    because git commands here exit non-zero as a matter of course -- ``rev-parse
    --verify --quiet`` on a branch that does not exist is the probe behind "does
    this branch already exist", so the answer became "your host cannot sandbox
    git" for every create.

    Deliberately does NOT take the ``repo`` fixture. That fixture skips wherever
    isolation cannot be established, which is every CI platform -- Windows has no
    backend at all and ubuntu-latest denies ``unshare(NEWNS)`` under AppArmor -- so
    the guard for the bug above would never run where it matters. Nothing here needs
    a repo or a sandbox: ``run_limited`` is stubbed, the spawn goes through
    ``_passthrough_spawn`` (the file's existing device for exactly this), and the cwd
    only has to be a real directory.
    """

    _WARNING = (
        "sandbox: WARNING — pre-exec hardlink scan truncated at 100000 files in "
        "/tmp; scan incomplete (control degrades open)"
    )
    _FATAL = "sandbox: unshare(NEWNS) failed: errno 1"

    @pytest.fixture(autouse=True)
    def _spawn_passthrough(self, monkeypatch):
        from kiro_crew.dashboard.handlers import worktree as wt

        monkeypatch.setattr(wt, "sandboxed_spawn_argv", _passthrough_spawn)

    def _completed(self, returncode: int, stderr: str):
        return subprocess.CompletedProcess(
            args=["git"], returncode=returncode, stdout="", stderr=stderr
        )

    def test_a_warning_on_a_non_zero_exit_is_returned_as_data(self, tmp_path, monkeypatch):
        """The non-zero exit is the ANSWER here, not an error to translate."""
        from kiro_crew.dashboard.handlers import worktree as wt

        stderr = self._WARNING + "\n"
        monkeypatch.setattr(wt, "run_limited", lambda *a, **k: self._completed(1, stderr))
        proc = _run_git(["rev-parse", "--verify", "--quiet", "refs/heads/nope"], str(tmp_path))

        assert proc.returncode == 1

    def test_a_fatal_launcher_line_still_refuses(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.handlers import worktree as wt

        stderr = self._FATAL + "\n"
        monkeypatch.setattr(wt, "run_limited", lambda *a, **k: self._completed(1, stderr))

        with pytest.raises(SandboxUnavailable):
            _run_git(["rev-parse", "HEAD"], str(tmp_path))

    def test_a_fatal_line_a_warning_precedes_is_still_found(self, tmp_path, monkeypatch):
        """Order must not decide it: ``startswith`` on the whole stderr missed this."""
        from kiro_crew.dashboard.handlers import worktree as wt

        stderr = f"{self._WARNING}\n{self._FATAL}\n"
        monkeypatch.setattr(wt, "run_limited", lambda *a, **k: self._completed(1, stderr))

        with pytest.raises(SandboxUnavailable) as excinfo:
            _run_git(["rev-parse", "HEAD"], str(tmp_path))

        # The FATAL line alone, not the whole stderr: the message reaches the user as
        # the reason the create was refused, and leading it with an advisory about
        # /tmp names the wrong cause.
        assert str(excinfo.value) == self._FATAL

    @pytest.mark.skipif(
        not hasattr(os, "getuid"),
        reason="the namespace launcher is POSIX-only: _build_launcher_script reads "
        "os.getuid(), which Windows has not got",
    )
    def test_the_launcher_emits_no_severity_the_classifier_does_not_know(self):
        """Ratchet the coupling: `_WARNING` above is a hand-typed copy of the real text.

        The classifier decides fatal-vs-advisory from one prefix, so a launcher line
        that is neither -- a reworded advisory (``sandbox: note - ...``), or a second
        non-fatal kind -- is read as a refusal, and on a busy-/tmp host every git
        command that legitimately exits non-zero again reports "this host has no OS
        sandbox backend". Nothing else pins that, because the tests above feed the
        classifier their own strings.

        Pinned as the SET of severities the launcher can emit, so adding one is a
        deliberate edit here plus a decision about which side of the prefix it falls
        on -- rather than a silent reclassification.
        """
        from kiro_crew.sandbox import _build_launcher_script

        severities = {
            token.split()[0]
            for token in re.findall(
                r"sandbox: ([^'\"%\\\n]+)", _build_launcher_script("strict")
            )
        }

        assert severities == {"unshare(NEWUSER)", "unshare(NEWNS)", "BLOCKED", "WARNING"}
        # And the one advisory is spelled the way the classifier looks for it.
        assert self._WARNING.startswith(wt_mod._SANDBOX_LAUNCHER_WARNING_PREFIX)
        assert wt_mod._SANDBOX_LAUNCHER_WARNING_PREFIX.startswith(
            wt_mod._SANDBOX_LAUNCHER_PREFIX
        ), "the advisory prefix must be a refinement of the launcher prefix, not a rival"

    def test_gits_own_error_is_never_mistaken_for_a_refusal(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.handlers import worktree as wt

        stderr = "fatal: not a git repository (or any of the parent directories): .git\n"
        monkeypatch.setattr(wt, "run_limited", lambda *a, **k: self._completed(128, stderr))
        proc = _run_git(["rev-parse", "HEAD"], str(tmp_path))

        assert proc.returncode == 128
