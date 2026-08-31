"""Tests for ``GET /api/project/git`` — branch label for the project chip.

Covers the known-project allow-list that keeps the route from being an
arbitrary-path prober, repo detection (including the worktree case where
``.git`` is a file), detached HEAD, the sensitive-path denial, and the
degrade-to-no-branch behaviour when git is unavailable or hangs.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import kiro_crew.dashboard.handlers.files as files_mod
from kiro_crew.dashboard.handlers import api_project_git
from kiro_crew.dashboard.handlers.files import (
    _GIT_ROOT_WALK_LIMIT,
    _HEAD_READ_LIMIT,
    _git_head_path,
    _known_project_dirs,
    _match_known_project,
    _match_known_project_for,
    _project_git_branch,
    _resolve_project_git,
    _slot_project_snapshot,
)
from kiro_crew.security import _looks_like_secret_key, redact


class _Slot:
    def __init__(self, project: str) -> None:
        self.project = project


class _State:
    """Minimal DashboardState stand-in exposing the slots the handler reads."""

    def __init__(self, *projects: str) -> None:
        self._slots = {f"s{i}": _Slot(p) for i, p in enumerate(projects)}


def _make_app(*known: str) -> web.Application:
    app = web.Application()
    app["state"] = _State(*known)
    app.router.add_get("/api/project/git", api_project_git)
    return app


@pytest.fixture()
def mock_sel():
    with patch("kiro_crew.dashboard.handlers.sel") as m:
        m.return_value = MagicMock()
        yield m.return_value


def _git(cwd, *args) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        # Identity is pinned here as well as in conftest's autouse ``_git_identity``,
        # which is FUNCTION-scoped and so does not cover the session-scoped template
        # builder below. os.devnull rather than a literal /dev/null: this file is
        # collected on Windows, where that path does not exist.
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "T",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "T",
            "GIT_COMMITTER_EMAIL": "t@example.com",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
        },
    )


@pytest.fixture(scope="session")
def _repo_template(tmp_path_factory):
    """Build the one-commit ``trunk`` repo once per session; ``repo`` copies it.

    The five git subprocesses below cost ~0.6s per test across 32 invocations. Session
    scope is safe because the template is never handed to a test, only copied from.
    """
    root = tmp_path_factory.mktemp("project-git-seed") / "proj"
    root.mkdir()
    _git(root, "init", "-q", "-b", "trunk")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    (root / "f.txt").write_text("x")
    _git(root, "add", "f.txt")
    _git(root, "commit", "-qm", "init")
    return root


@pytest.fixture()
def repo(tmp_path, _repo_template):
    """A real git repo with one commit on branch ``trunk``."""
    root = tmp_path / "proj"
    shutil.copytree(_repo_template, root)
    return root


class TestProjectGitEndpoint:
    @pytest.mark.asyncio
    async def test_returns_branch_for_repo(self, repo, mock_sel):
        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.get("/api/project/git", params={"path": str(repo)})
            data = await resp.json()
        assert resp.status == 200
        assert data["branch"] == "trunk"
        assert data["repoRoot"] == os.path.realpath(str(repo))

    @pytest.mark.asyncio
    async def test_finds_repo_root_from_subdirectory(self, repo, mock_sel):
        nested = repo / "src" / "pkg"
        nested.mkdir(parents=True)
        async with TestClient(TestServer(_make_app(str(nested)))) as client:
            resp = await client.get("/api/project/git", params={"path": str(nested)})
            data = await resp.json()
        assert resp.status == 200
        assert data["branch"] == "trunk"
        assert data["repoRoot"] == os.path.realpath(str(repo))

    @pytest.mark.asyncio
    async def test_non_repo_reports_repo_false(self, tmp_path, mock_sel):
        plain = tmp_path / "plain"
        plain.mkdir()
        async with TestClient(TestServer(_make_app(str(plain)))) as client:
            resp = await client.get(f"/api/project/git?path={plain}")
            data = await resp.json()
        # tmp_path can sit under an unrelated repo on some hosts; assert only
        # that no branch is claimed for a directory with no .git of its own.
        assert data.get("branch", "") != "trunk"
        assert "path" in data

    @pytest.mark.asyncio
    async def test_unknown_directory_is_refused(self, repo, tmp_path, mock_sel):
        """The route must not answer for a path the gateway does not know."""
        other = tmp_path / "somewhere-else"
        other.mkdir()
        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.get(f"/api/project/git?path={other}")
            assert resp.status == 403
            body = await resp.json()
        assert "Unknown project" in body["error"]
        kwargs = mock_sel.log_api_access.call_args.kwargs
        assert kwargs["outcome"] == "denied"
        assert kwargs["error"] == "not a known project directory"

    @pytest.mark.asyncio
    async def test_traversal_out_of_a_known_project_is_refused(self, repo, mock_sel):
        """`<known>/../..` normalises outside the allow-list and must not match."""
        async with TestClient(TestServer(_make_app(str(repo)))) as client:
            resp = await client.get(f"/api/project/git?path={repo}/../..")
            assert resp.status == 403

    @pytest.mark.asyncio
    async def test_recent_projects_entry_is_accepted(self, repo, tmp_path, mock_sel):
        """A picker-recorded project is allowed even with no live slot for it."""
        cfg = tmp_path / "cfg"
        cfg.mkdir()
        (cfg / "recent_projects.json").write_text(json.dumps([str(repo)]), encoding="utf-8")
        with patch("kiro_crew.dashboard.handlers.files.config_dir", return_value=cfg):
            async with TestClient(TestServer(_make_app())) as client:
                resp = await client.get(f"/api/project/git?path={repo}")
                assert resp.status == 200
                data = await resp.json()
        assert data["branch"] == "trunk"

    @pytest.mark.asyncio
    async def test_missing_path_is_400(self, mock_sel):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/api/project/git")
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_not_a_directory_is_400(self, repo, mock_sel):
        f = repo / "f.txt"
        async with TestClient(TestServer(_make_app(str(f)))) as client:
            resp = await client.get(f"/api/project/git?path={f}")
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_sensitive_path_is_denied(self, repo, mock_sel):
        with patch("kiro_crew.dashboard.handlers.files.is_sensitive_path", return_value=True):
            async with TestClient(TestServer(_make_app(str(repo)))) as client:
                resp = await client.get(f"/api/project/git?path={repo}")
                assert resp.status == 403
        assert mock_sel.log_api_access.call_args.kwargs["outcome"] == "denied"


class TestKnownProjectMatching:
    def test_returns_the_server_held_string_not_the_callers(self):
        """The matched value must be the allow-list entry, never request text."""
        known = ["/srv/work/proj"]
        assert _match_known_project("/srv/work/proj/", known) == "/srv/work/proj"
        assert _match_known_project("/srv/work/./proj", known) == "/srv/work/proj"
        assert _match_known_project("/srv/work/other/../proj", known) == "/srv/work/proj"

    def test_non_member_returns_none(self):
        assert _match_known_project("/etc", ["/srv/work/proj"]) is None
        assert _match_known_project("/srv/work", ["/srv/work/proj"]) is None
        # A path merely PREFIXED by a known project is not itself known.
        assert _match_known_project("/srv/work/project-x", ["/srv/work/proj"]) is None

    def test_empty_allowlist_matches_nothing(self):
        assert _match_known_project("/srv/work/proj", []) is None

    def test_slots_and_recent_are_both_collected(self, tmp_path):
        cfg = tmp_path / "cfg"
        cfg.mkdir()
        (cfg / "recent_projects.json").write_text(json.dumps(["/from/recent", 7]), encoding="utf-8")
        with patch("kiro_crew.dashboard.handlers.files.config_dir", return_value=cfg):
            dirs = _known_project_dirs(_slot_project_snapshot(_State("/from/slot", "")))
        assert "/from/slot" in dirs
        assert "/from/recent" in dirs
        assert "" not in dirs  # empty slot project contributes nothing
        assert 7 not in dirs  # non-string recent entries are dropped

    def test_corrupt_recent_file_degrades_to_slots_only(self, tmp_path):
        cfg = tmp_path / "cfg"
        cfg.mkdir()
        (cfg / "recent_projects.json").write_text("{not json", encoding="utf-8")
        with patch("kiro_crew.dashboard.handlers.files.config_dir", return_value=cfg):
            dirs = _known_project_dirs(_slot_project_snapshot(_State("/from/slot")))
        assert dirs == ["/from/slot"]


class TestProjectGitBranchResolver:
    def test_worktree_gitfile_is_a_repo(self, repo, tmp_path):
        """A worktree's ``.git`` is a FILE holding ``gitdir: <path>``, and that
        directory holds the worktree's own HEAD."""
        wt = tmp_path / "wt"
        _git(repo, "worktree", "add", "-q", "-b", "feat/x", str(wt))
        assert (wt / ".git").is_file()
        info = _project_git_branch(os.path.realpath(str(wt)))
        assert info["repo"] is True
        assert info["branch"] == "feat/x"

    def test_detached_head_reports_short_sha(self, repo):
        full = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        _git(repo, "checkout", "-q", "--detach", "HEAD")
        info = _project_git_branch(os.path.realpath(str(repo)))
        assert info["detached"] is True
        assert info["head"] == full[:7]
        assert "branch" not in info

    def test_walk_up_is_depth_bounded(self, repo):
        """A path nested past the ceiling is not walked all the way to the root.

        Segments are one character and the depth is derived from the limit rather
        than a padded literal, because the whole tree has to fit inside Windows'
        260-character MAX_PATH: multi-character names at a hardcoded depth of 45
        pushed the leaf to 272 characters under pytest's tmp dir and ``mkdir``
        failed with WinError 206 before the assertion was ever reached.
        """
        depth = _GIT_ROOT_WALK_LIMIT + 5
        deep = repo.joinpath(*["d"] * depth)
        deep.mkdir(parents=True)
        info = _project_git_branch(os.path.realpath(str(deep)))
        assert info == {"repo": False}


class TestNoGitSubprocess:
    """The branch is read from ``.git/HEAD`` directly — no git process runs.

    A git invocation parses the repository's own config, and repo-local config
    can contain ``[include] path = <any file>``, which makes git READ that file.
    For a project directory the agent can select, that is an arbitrary-file-read
    primitive. Reading HEAD ourselves removes the whole class: no config is
    parsed, no binary is resolved, nothing is spawned.
    """

    def test_no_subprocess_is_spawned(self, repo):
        with patch("subprocess.run", side_effect=AssertionError("spawned a process")):
            info = _project_git_branch(os.path.realpath(str(repo)))
        assert info["branch"] == "trunk"

    def test_config_include_directive_is_never_read(self, repo, tmp_path):
        """The exact mechanism from the review finding.

        The planted file is deliberately named and worded neutrally: what matters
        is that a git invocation reads an ARBITRARY caller-named file, not what
        that file happens to contain. Dressing the fixture up as a fake secret
        adds nothing and trips the clear-text-storage scanner.
        """
        included = tmp_path / "included.cfg"
        included.write_text("[probe]\n\tmarker = INCLUDE-WAS-PARSED\n")
        with open(repo / ".git" / "config", "a", encoding="utf-8") as fh:
            # Forward slashes: git config treats a backslash as an escape, so a
            # native Windows path would not resolve.
            fh.write(f"[include]\n\tpath = {included.as_posix()}\n")
        # Confirm the vector is real for a git invocation...
        probe = subprocess.run(
            ["git", "-C", str(repo), "config", "--get", "probe.marker"],
            capture_output=True, text=True, check=False,
        )
        assert probe.stdout.strip() == "INCLUDE-WAS-PARSED", "include vector not reproduced"
        # ...and that our reader is unaffected by it and reads no config at all.
        info = _project_git_branch(os.path.realpath(str(repo)))
        assert info["branch"] == "trunk"
        assert "INCLUDE-WAS-PARSED" not in repr(info)

    def test_unreadable_head_degrades_to_no_branch(self, repo):
        (repo / ".git" / "HEAD").unlink()
        info = _project_git_branch(os.path.realpath(str(repo)))
        assert info["repo"] is True
        assert "branch" not in info and "head" not in info

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink semantics")
    def test_symlinked_head_to_a_blocked_path_is_refused(self, repo, tmp_path):
        """`.git/HEAD` is an ordinary path and can be a symlink to anything.

        A hex secret is the dangerous shape: it would otherwise match the
        detached-HEAD pattern and get echoed as a 7-char prefix. Reads go through
        the hooks sensitive-path gate, so a refused target yields no label.
        """
        target = tmp_path / "blocked.txt"
        target.write_text("a" * 40 + "\n")
        head = repo / ".git" / "HEAD"
        head.unlink()
        head.symlink_to(target)
        with patch(
            "kiro_crew.dashboard.handlers.files.safe_read_prefix", return_value=None
        ) as gated:
            info = _project_git_branch(os.path.realpath(str(repo)))
        assert gated.called, "HEAD read did not go through the hooks gate"
        assert "head" not in info and "branch" not in info
        assert info["repo"] is True

    def test_head_read_routes_through_the_hooks_gate(self, repo):
        """Pins the gate itself, independent of any particular symlink."""
        with patch(
            "kiro_crew.dashboard.handlers.files.safe_read_prefix", return_value=None
        ) as gated:
            info = _project_git_branch(os.path.realpath(str(repo)))
        assert gated.called
        assert "branch" not in info

    def test_garbage_head_is_not_reported_as_a_branch(self, repo):
        (repo / ".git" / "HEAD").write_text("not a ref and not a sha\n")
        info = _project_git_branch(os.path.realpath(str(repo)))
        assert info["repo"] is True
        assert "branch" not in info and "detached" not in info

    def test_non_branch_symbolic_ref_is_not_labelled(self, repo):
        """Only refs/heads/* is a branch; a bare or tag ref must not be shown."""
        (repo / ".git" / "HEAD").write_text("ref: refs/heads/\n")
        assert "branch" not in _project_git_branch(os.path.realpath(str(repo)))
        (repo / ".git" / "HEAD").write_text("ref: refs/tags/v1\n")
        assert "branch" not in _project_git_branch(os.path.realpath(str(repo)))

    def test_head_read_is_size_capped(self, repo):
        """A hostile oversized HEAD must not be slurped whole."""
        (repo / ".git" / "HEAD").write_text("ref: refs/heads/" + ("x" * 100000))
        info = _project_git_branch(os.path.realpath(str(repo)))
        assert len(info.get("branch", "")) <= _HEAD_READ_LIMIT

    def test_worktree_gitdir_pointer_must_be_a_gitdir_line(self, repo, tmp_path):
        wt = tmp_path / "bogus"
        wt.mkdir()
        (wt / ".git").write_text("something else entirely\n")
        assert _git_head_path(str(wt)) is None


class TestMatchKnownProjectOffLoop:
    def test_matches_via_the_worker_thread_helper(self, repo):
        assert _match_known_project_for(_slot_project_snapshot(_State(str(repo))), str(repo)) == str(repo)

    def test_tilde_form_is_matched_without_touching_the_loop(self):
        """`expanduser` on a ~user form does a passwd lookup; it must run in the
        thread helper, and an unknown user must simply not match."""
        assert _match_known_project_for(["/srv/p"], "~nosuchuser42/x") is None

    def test_unknown_is_none(self, repo):
        assert _match_known_project_for(_slot_project_snapshot(_State(str(repo))), "/etc") is None


class TestBranchRedaction:
    """The branch label is echoed to the dashboard and is copyable, so it goes
    through the canonical egress redaction like any other returned string."""

    def test_ordinary_branch_names_pass_through_unchanged(self, repo):
        info = _project_git_branch(os.path.realpath(str(repo)))
        assert info["branch"] == "trunk"

    def test_macos_temp_root_is_not_mistaken_for_a_bare_secret(self):
        path = "/private/var/folders/6r/54rts88h7yebke7n6clhoq9d0roaen/T/project"
        assert files_mod._redact_project_path(path) == path

    def test_macos_temp_root_still_redacts_a_secret_in_the_suffix(self):
        key = "AKIAIOSFODNN7EXAMPLE"
        path = (
            "/private/var/folders/6r/54rts88h7yebke7n6clhoq9d0roaen/T/"
            f"project/{key}"
        )
        redacted = files_mod._redact_project_path(path)
        assert key not in redacted
        assert "[REDACTED:" in redacted

    def test_other_paths_still_use_the_canonical_redactor(self):
        with patch(
            "kiro_crew.dashboard.handlers.files.redact", return_value="masked"
        ) as red:
            assert files_mod._redact_project_path("/srv/project") == "masked"
        red.assert_called_once_with("/srv/project")

    def test_similar_macos_path_with_the_wrong_id_width_is_not_exempt(self):
        path = "/private/var/folders/6r/54rts88h7yebke7n6clhoq9d0roaenx/T/project"
        with patch(
            "kiro_crew.dashboard.handlers.files.redact", return_value="masked"
        ) as red:
            assert files_mod._redact_project_path(path) == "masked"
        red.assert_called_once_with(path)

    def test_branch_name_is_routed_through_redaction(self, repo):
        with patch(
            "kiro_crew.dashboard.handlers.files.redact", side_effect=lambda t: f"<{t}>"
        ) as red:
            info = _project_git_branch(os.path.realpath(str(repo)))
        assert info["branch"] == "<trunk>"
        # repoRoot is redacted too, so assert the branch call specifically rather
        # than a total call count.
        red.assert_any_call("trunk")

    def test_detached_head_sha_is_routed_through_redaction(self, repo):
        _git(repo, "checkout", "-q", "--detach", "HEAD")
        with patch(
            "kiro_crew.dashboard.handlers.files.redact", side_effect=lambda t: f"<{t}>"
        ):
            info = _project_git_branch(os.path.realpath(str(repo)))
        assert info["detached"] is True
        assert info["head"].startswith("<") and info["head"].endswith(">")

    def test_repo_root_is_routed_through_redaction(self, repo):
        """A directory NAME is agent-influenceable via set_project and is echoed."""
        with patch(
            "kiro_crew.dashboard.handlers.files.redact", side_effect=lambda t: f"<{t}>"
        ):
            info = _project_git_branch(os.path.realpath(str(repo)))
        assert "<" in info["repoRoot"] and info["repoRoot"].endswith(">")

    @pytest.mark.asyncio
    async def test_not_a_directory_response_path_is_redacted(self, repo, mock_sel):
        """Reachable whenever a known project is deleted between match and stat."""
        f = repo / "f.txt"
        with patch(
            "kiro_crew.dashboard.handlers.files.redact", side_effect=lambda t: f"<{t}>"
        ):
            async with TestClient(TestServer(_make_app(str(f)))) as client:
                resp = await client.get(f"/api/project/git?path={f}")
                assert resp.status == 400
                data = await resp.json()
        assert "<" in data["path"] and data["path"].endswith(">")

    @pytest.mark.asyncio
    async def test_no_response_echoes_an_unredacted_path(self, repo, tmp_path, mock_sel):
        """Class guard: every response body that carries a path must redact it.

        Added after a fix redacted the success path but left the 400 arm raw.
        """
        marker = "REDACTED-SENTINEL"
        cases = [
            (str(repo), f"/api/project/git?path={repo}"),                 # 200
            (str(repo / "f.txt"), f"/api/project/git?path={repo / 'f.txt'}"),  # 400
            (str(repo), f"/api/project/git?path={tmp_path / 'nope'}"),    # 403 unknown
        ]
        for known, url in cases:
            with patch(
                "kiro_crew.dashboard.handlers.files.redact", return_value=marker
            ):
                async with TestClient(TestServer(_make_app(known))) as client:
                    resp = await client.get(url)
                    body = await resp.text()
            # Any path-shaped value in the body must have gone through redact().
            assert str(repo) not in body, f"raw path leaked in {resp.status}: {body}"

    @pytest.mark.asyncio
    async def test_response_path_is_routed_through_redaction(self, repo, mock_sel):
        with patch(
            "kiro_crew.dashboard.handlers.files.redact", side_effect=lambda t: f"<{t}>"
        ):
            async with TestClient(TestServer(_make_app(str(repo)))) as client:
                resp = await client.get(f"/api/project/git?path={repo}")
                data = await resp.json()
        assert "<" in data["path"] and data["path"].endswith(">")
        # The SEL audit still records the real, unredacted path.
        assert mock_sel.log_api_access.call_args.kwargs["resources"] == os.path.realpath(
            str(repo)
        )


class TestMacosPrefixBoundary:
    """What the macOS exemption may and may not cost the bare-secret detector.

    The exemption withholds the OS-owned ``[a-z0-9]{2}/[a-z0-9_]{30}`` id from
    the scan -- that is what stops it lending its entropy to a window and
    reviving the false positive. It does NOT withhold the trailing ``T``: an AWS
    secret key may contain ``/``, so ``T/`` plus 38 user-controlled characters is
    a well-formed 40-byte key rather than a window that merely borrows OS bytes.
    These pin the resulting property -- the canonical output policy is preserved
    over the whole egress string, while the id itself is never scanned.
    """

    # An ordinary Darwin temp id, which carries an underscore. The underscore is
    # outside the bare-secret character class, so this prefix is NOT itself
    # mistaken for a secret -- the boundary property is therefore measured
    # independently of the false positive this PR exists to fix.
    CLEAN = "/private/var/folders/6r/zyxvpxvq6csfxvn_n0000000000000/T"
    # An all-alphanumeric id, which IS self-flagged. This is the #6905 defect.
    SELF_FLAGGED = "/private/var/folders/6r/54rts88h7yebke7n6clhoq9d0roaen/T"

    # Reaches 40 characters only by borrowing the prefix's `T` and `/`.
    NAME_38 = "doDgQR96cI1L6Y9sI5uY15bnF8NJQHuYGHR93L"
    # Reaches 40 characters using only its own leading separator.
    NAME_39 = "l2ItjQWkjQZ7O857spPui2ot3weV2dKl3pVyk13"

    # A genuine AWS-shaped secret key that begins ``T/``. The canonical
    # classifier accepts it standing alone, so the 38 characters after ``T/``
    # are a credential TAIL, not an innocuous directory name that happens to
    # reach 40 bytes by borrowing the OS prefix.
    BOUNDARY_KEY = "T/PtYgjmUhBel31iEl2hpChYgCfrL1spNxnyVmih"
    # ... and one that starts one byte further left, on the separator BEFORE the
    # `T`. Both bytes are fixed literals in the prefix regex, so both windows are
    # composed entirely of fixed or user-controlled bytes and both must be seen.
    SLASH_BOUNDARY_KEY = "/T/EqV8ib8HDy88YtDtXbiufMdI8X2Y4rUmer/BH"

    def test_a_credential_spanning_the_prefix_boundary_is_redacted(self):
        """The blocking finding, pinned.

        Splitting the scan at ``match.end()`` produced a class where the
        canonical redactor removed a value and the path-aware helper did not --
        the helper weakened the output policy rather than narrowing a false
        positive. Both halves are asserted, so a regression cannot pass by
        making the canonical side stop firing either.
        """
        for key, lead in (
            (self.BOUNDARY_KEY, "T/"),
            (self.SLASH_BOUNDARY_KEY, "/T/"),
        ):
            assert _looks_like_secret_key(key), (
                "fixture must be a credential by the canonical classifier"
            )
            tail = key[len(lead) :]
            assert len(key) == 40 and len(tail) == 40 - len(lead)
            for prefix in (self.CLEAN, self.SELF_FLAGGED):
                path = f"{prefix}/{tail}"
                assert redact(path) != path, "canonical redactor must remove it"
                out = files_mod._redact_project_path(path)
                assert tail not in out, f"boundary credential survived: {out}"
                assert "[REDACTED:" in out

    def test_the_two_fixture_prefixes_behave_as_documented(self):
        assert redact(self.CLEAN) == self.CLEAN
        assert redact(self.SELF_FLAGGED) != self.SELF_FLAGGED
        assert files_mod._redact_project_path(self.SELF_FLAGGED) == self.SELF_FLAGGED

    def test_neither_boundary_name_is_a_secret_standing_alone(self):
        """Neither value is a secret by itself: both are under 40 characters.

        They differ only in what their own leading separator buys them. The
        39-character name reaches a 40-character window with the `/` that the
        suffix scan already sees; the 38-character one can only get there by
        borrowing the OS-owned `T` as well, which is the window the exemption
        gives up.
        """
        for name in (self.NAME_38, self.NAME_39):
            assert len(name) < 40
            assert redact(name) == name
        assert redact(f"/{self.NAME_38}") == f"/{self.NAME_38}"
        assert redact(f"/{self.NAME_39}") != f"/{self.NAME_39}"

    def test_a_38_char_name_is_judged_on_its_own_boundary_window(self):
        """Being under 40 characters is NOT what decides a 38-char name.

        The earlier contract kept every such name on the reasoning that it sits
        below the classifier's exact-40 minimum. That reasoning does not hold:
        the window the classifier actually evaluates is ``T/`` plus the name, and
        whether THAT is a credential is a per-value question. So the name is
        handed to the canonical classifier with its boundary and judged there --
        this fixture clears the gates and is removed.
        """
        assert _looks_like_secret_key(f"T/{self.NAME_38}")
        path = f"{self.CLEAN}/{self.NAME_38}"
        assert redact(path) != path
        out = files_mod._redact_project_path(path)
        assert self.NAME_38 not in out
        # And the exemption is still doing its job: the OS-owned id above the
        # boundary is preserved byte-for-byte, never scanned.
        assert out.startswith(self.CLEAN[: -len("/T")])
        assert "[REDACTED:" in out

    def test_ordinary_project_names_survive_under_both_prefixes(self):
        """The false positive #6905 exists to fix, including the new boundary.

        The ``T`` enters the scan, so this is the assertion that would break if
        the boundary context were widened any further -- one more character
        drags the high-entropy OS id in with it.
        """
        for prefix in (self.CLEAN, self.SELF_FLAGGED):
            for name in (
                "project",
                "kiro-crew",
                "my_app-2",
                "pytest-of-user/pytest-3/test_thing0",
                "a" * 20,
                "0123456789abcdef0123456789abcdef012345",
                # 37 hex chars: the length that reaches 40 only with `/T/`, i.e.
                # exactly the window the second boundary byte adds.
                "0123456789abcdef0123456789abcdef01234",
            ):
                path = f"{prefix}/{name}"
                assert files_mod._redact_project_path(path) == path, path
        # ... and the prefix alone, with no suffix at all.
        for prefix in (self.CLEAN, self.SELF_FLAGGED):
            assert files_mod._redact_project_path(prefix) == prefix

    def test_the_os_owned_id_never_enters_the_scan(self):
        """Why the exemption still exists at all.

        ``SELF_FLAGGED`` is an all-alphanumeric Darwin id that the canonical
        redactor removes on its own account. Withholding it is the whole fix; if
        a future change fed it to the scan, this fails.
        """
        assert redact(self.SELF_FLAGGED) != self.SELF_FLAGGED
        assert files_mod._redact_project_path(self.SELF_FLAGGED) == self.SELF_FLAGGED
        assert (
            files_mod._redact_project_path(f"{self.SELF_FLAGGED}/project")
            == f"{self.SELF_FLAGGED}/project"
        )

    def test_a_39_char_name_is_still_scanned_with_its_separator(self):
        """One character longer and the suffix scan reaches 40 unaided.

        This is what bounds the loss: the suffix always begins at the `/` that
        follows the OS-owned `T`, so a user-controlled run only has to reach 39
        characters to be scanned as a 40-character window.
        """
        for prefix in (self.CLEAN, self.SELF_FLAGGED):
            out = files_mod._redact_project_path(f"{prefix}/{self.NAME_39}")
            assert self.NAME_39 not in out
            assert "[REDACTED:" in out

    @pytest.mark.parametrize(
        "value",
        [
            "AKIAIOSFODNN7EXAMPLE",
            "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            "ghp_A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8",
        ],
    )
    @pytest.mark.parametrize(
        "layout", ["{v}", "project/{v}", "{v}/sub", "a/b/{v}/c"]
    )
    def test_a_real_credential_never_survives_the_split(self, value, layout):
        """The invariant the exemption must not break, at every placement."""
        assert redact(value) != value, "fixture must be detectable standing alone"
        for prefix in (self.CLEAN, self.SELF_FLAGGED):
            path = f"{prefix}/{layout.format(v=value)}"
            assert value not in files_mod._redact_project_path(path)

    def test_the_exemption_is_exactly_prefix_plus_canonical_suffix(self):
        """The whole contract, stated as an identity rather than a behaviour.

        Everything above the trailing ``/T`` is preserved byte-for-byte; the
        ``/T`` and everything after it are handed to the canonical redactor
        untouched. Nothing else is decided here, so the exemption cannot drift
        into a second redaction policy: any future change to `redact()` applies
        to the scanned part automatically.

        The two preserved-vs-scanned byte counts are asserted, not assumed: the
        boundary is the END of the OS-generated id, and ``/T`` is the fixed
        literal the prefix regex ends with.
        """
        for prefix in (self.CLEAN, self.SELF_FLAGGED):
            head, boundary = prefix[:-2], prefix[-2:]
            assert boundary == "/T"
            for tail in (
                "",
                "/project",
                f"/{self.NAME_38}",
                f"/{self.NAME_39}",
                f"/{self.BOUNDARY_KEY[len('T/'):]}",
                f"/{self.SLASH_BOUNDARY_KEY[len('/T/'):]}",
                "/wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            ):
                path = prefix + tail
                assert files_mod._redact_project_path(path) == head + redact(
                    boundary + tail
                )


class TestSlotSnapshotOffLoop:
    """The worker thread must never iterate the live slot map.

    Slots are created and deleted by other coroutines on the event loop, so a
    worker-thread iteration can hit `RuntimeError: dictionary changed size` and
    turn a decorative branch poll into an HTTP 500. The snapshot is taken on the
    loop, where those mutations are serialised against it.
    """

    def test_snapshot_collects_non_empty_slot_projects(self):
        assert _slot_project_snapshot(_State("/a", "", "/b")) == ["/a", "/b"]

    def test_snapshot_tolerates_missing_slots_attribute(self):
        class Bare:
            pass

        assert _slot_project_snapshot(Bare()) == []

    @pytest.mark.asyncio
    async def test_snapshot_is_taken_on_the_event_loop_not_the_worker(
        self, repo, mock_sel
    ):
        """Pins WHERE the copy happens, which is the whole point of the fix.

        Asserting the worker merely receives a list does not discriminate — a
        snapshot taken inside the thread also produces a list. Comparing thread
        identities does.
        """
        loop_tid = threading.get_ident()
        seen: dict = {}
        real_snap = files_mod._slot_project_snapshot
        real_match = files_mod._match_known_project_for

        def _snap(state):
            seen["snapshot_tid"] = threading.get_ident()
            return real_snap(state)

        def _match(slot_projects, raw):
            seen["match_tid"] = threading.get_ident()
            return real_match(slot_projects, raw)

        with (
            patch.object(files_mod, "_slot_project_snapshot", _snap),
            patch.object(files_mod, "_match_known_project_for", _match),
        ):
            async with TestClient(TestServer(_make_app(str(repo)))) as client:
                resp = await client.get(f"/api/project/git?path={repo}")
                assert resp.status == 200
        assert seen["snapshot_tid"] == loop_tid, "slot snapshot ran off the event loop"
        assert seen["match_tid"] != loop_tid, "matcher ran ON the event loop"

    @pytest.mark.asyncio
    async def test_worker_receives_a_snapshot_not_the_state(self, repo, mock_sel):
        """Pins the contract: what crosses into the thread is a plain list."""
        seen: list = []
        real = files_mod._match_known_project_for

        def _capture(slot_projects, raw):
            seen.append(slot_projects)
            return real(slot_projects, raw)

        with patch.object(files_mod, "_match_known_project_for", _capture):
            async with TestClient(TestServer(_make_app(str(repo)))) as client:
                resp = await client.get(f"/api/project/git?path={repo}")
                assert resp.status == 200
        assert seen, "matcher was never called"
        assert isinstance(seen[0], list), f"worker got {type(seen[0]).__name__}, not a list"
        assert seen[0] == [str(repo)]

    @pytest.mark.asyncio
    async def test_slot_mutation_during_the_poll_does_not_500(self, repo, mock_sel):
        """A slot deleted while the worker runs must not break the request."""
        app = _make_app(str(repo))
        state = app["state"]

        def _mutate_then_match(slot_projects, raw):
            # Stand in for another coroutine deleting a slot mid-poll. The worker
            # holds a snapshot, so this must not affect it.
            state._slots.clear()
            return _match_known_project_for(slot_projects, raw)

        with patch.object(files_mod, "_match_known_project_for", _mutate_then_match):
            async with TestClient(TestServer(app)) as client:
                resp = await client.get(f"/api/project/git?path={repo}")
                assert resp.status == 200
                data = await resp.json()
        assert data["branch"] == "trunk"


class TestResolveProjectGit:
    """``_resolve_project_git`` carries every filesystem probe off the loop."""

    def test_ok_returns_base_and_branch(self, repo):
        status, base, info = _resolve_project_git(str(repo))
        assert status == "ok"
        assert base == os.path.realpath(str(repo))
        assert info["branch"] == "trunk"

    def test_non_directory_is_reported(self, repo):
        status, _base, info = _resolve_project_git(str(repo / "f.txt"))
        assert status == "not_a_dir"
        assert info == {}

    def test_sensitive_path_is_reported_without_running_git(self, repo):
        with (
            patch("kiro_crew.dashboard.handlers.files.is_sensitive_path", return_value=True),
            patch("kiro_crew.dashboard.handlers.files._project_git_branch") as branch,
        ):
            status, _base, info = _resolve_project_git(str(repo))
        assert status == "sensitive"
        assert info == {}
        branch.assert_not_called()
