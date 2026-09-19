"""GitLab draft-MR recipe — the provider twin of the GitHub recipe.

Mirrors ``test_pr_recipe.py``: the recipe must never publish, must never push to a
protected branch, and must degrade to the durable queue rather than losing a
verified change. The provider-selection seams (``build_pr_recipe`` and
``_provider_of``) are covered here too, since this recipe is why they exist.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from kiro_crew.apps.builtins.auto_improvement.profiles import (
    _provider_of,
    build_pr_recipe,
    prefer_authenticated_remote,
    target_provider,
)
from kiro_crew.apps.builtins.auto_improvement.profiles.gitlab_repo import mr_recipe as mr
from kiro_crew.apps.builtins.auto_improvement.spine.profile import PRRecipe


def _recipe(tmp_path: Path, **kw) -> mr.GitLabMRRecipe:
    return mr.GitLabMRRecipe(
        user="zedmor",
        clone_path=tmp_path / "clone",
        pr_queue_dir=tmp_path / "queue",
        base_ref=kw.pop("base_ref", "origin/main"),
        **kw,
    )


class TestProtocolConformance:
    def test_satisfies_the_spine_seam(self, tmp_path: Path) -> None:
        """Structural typing: the spine consumes the profile only through this."""
        assert isinstance(_recipe(tmp_path), PRRecipe)

    def test_namespace_is_metadata_only(self, tmp_path: Path) -> None:
        assert _recipe(tmp_path).namespace == "gitlab/zedmor"

    def test_base_ref_strips_remote_prefix(self, tmp_path: Path) -> None:
        """``glab --target-branch`` wants a plain branch name."""
        assert _recipe(tmp_path, base_ref="origin/develop").base_branch == "develop"

    def test_shares_the_push_machinery_with_the_github_recipe(self) -> None:
        """Anti-drift: the safety-relevant half must be ONE implementation, so a fix
        to the scan/push path can never land on one provider and miss the other."""
        from kiro_crew.apps.builtins.auto_improvement.profiles.github_repo.pr_recipe import (
            GitHubPRRecipe,
        )
        from kiro_crew.apps.builtins.auto_improvement.profiles.pr_recipe_base import (
            ProviderPRRecipe,
        )

        for name in ("_push_fix_branch", "_scan_pushable_content", "_scannable_base", "draft"):
            assert getattr(mr.GitLabMRRecipe, name) is getattr(ProviderPRRecipe, name)
            assert getattr(GitHubPRRecipe, name) is getattr(ProviderPRRecipe, name)


class TestExtractMrUrl:
    def test_finds_url_among_trailing_chatter(self) -> None:
        """Do NOT trust the last line: git hooks and agent stdout print after it."""
        out = "Warning: hook\nhttps://gitlab.com/o/r/-/merge_requests/7\nremote: done\n"
        assert mr.extract_mr_url(out) == "https://gitlab.com/o/r/-/merge_requests/7"

    def test_accepts_nested_group_paths(self) -> None:
        """GitLab projects nest (``group/subgroup/project``) — the path is not two
        fixed segments like a GitHub ``owner/repo``."""
        url = "https://gitlab.example.com/group/sub/project/-/merge_requests/123"
        assert mr.extract_mr_url(f"created!\n{url}\n") == url

    def test_rejects_prose(self) -> None:
        assert mr.extract_mr_url("breaking it up into smaller components.") is None

    def test_rejects_a_github_pull_url(self) -> None:
        """The ``/-/merge_requests/`` marker is what keeps the providers apart."""
        assert mr.extract_mr_url("https://github.com/o/r/pull/7") is None

    def test_requires_a_project_path(self) -> None:
        assert mr.extract_mr_url("https://gitlab.com/-/merge_requests/5") is None


class TestDraftOnlyPolicy:
    def test_command_is_draft_and_never_publishes(self) -> None:
        """``--draft`` is the mechanical half of the draft-only policy."""
        assert "--draft" in mr.DRAFT_MR_CMD
        joined = " ".join(mr.DRAFT_MR_CMD)
        for forbidden in ("merge", "ready", "approve", "--web"):
            assert forbidden not in joined

    def test_successful_draft_returns_the_url_with_the_required_flags(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(mr.shutil, "which", lambda _n: "/usr/bin/glab")
        monkeypatch.setattr(
            mr.GitLabMRRecipe,
            "_push_fix_branch",
            lambda self, *, branch: (True, branch),
        )
        seen: dict[str, list[str]] = {}

        def fake_run(cmd, **kw):  # noqa: ANN001
            seen["cmd"] = list(cmd)
            return subprocess.CompletedProcess(
                cmd, 0, "https://gitlab.com/g/sub/p/-/merge_requests/99\n", ""
            )

        monkeypatch.setattr(mr.subprocess, "run", fake_run)
        recipe = _recipe(tmp_path)
        out = recipe.draft(summary="perf: faster", description="body", diff="d", fingerprint="ca")
        assert out == "https://gitlab.com/g/sub/p/-/merge_requests/99"
        cmd = seen["cmd"]
        assert cmd[0] == "glab"
        # The MR must be a draft, headless, on a generated source branch, and must
        # target the configured base.
        assert "--draft" in cmd
        assert "--yes" in cmd
        assert "--description-file" in cmd
        assert "--title" in cmd
        assert "--source-branch" in cmd
        assert "--target-branch" in cmd and "main" in cmd


class TestDraftDegradation:
    def test_queue_copy_written_when_glab_is_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The durable record must survive a total drafting failure."""
        monkeypatch.setattr(mr.shutil, "which", lambda _n: None)  # no glab on PATH
        recipe = _recipe(tmp_path)
        result = recipe.draft(
            summary="perf: speed up the parser",
            description="# perf: speed up the parser\n\nEvidence...",
            diff="--- a\n+++ b\n",
            fingerprint="deadbeef",
        )
        assert result == "QUEUED:deadbeef"
        assert (tmp_path / "queue" / "deadbeef.diff").read_text() == "--- a\n+++ b\n"
        body = (tmp_path / "queue" / "deadbeef.pr.md").read_text()
        # The summary becomes the title, so the body's duplicate H1 is dropped.
        assert body.count("# perf: speed up the parser") == 1

    def test_a_nonzero_glab_exit_degrades_to_queue(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(mr.shutil, "which", lambda _n: "/usr/bin/glab")
        monkeypatch.setattr(
            mr.GitLabMRRecipe,
            "_push_fix_branch",
            lambda self, *, branch: (True, branch),
        )

        def fake_run(cmd, **kw):  # noqa: ANN001
            return subprocess.CompletedProcess(cmd, 1, "", "glab: 401 unauthorized")

        monkeypatch.setattr(mr.subprocess, "run", fake_run)
        recipe = _recipe(tmp_path)
        assert (
            recipe.draft(summary="fix: thing", description="body", diff="d", fingerprint="ff03")
            == "QUEUED:ff03"
        )

    def test_no_pushable_remote_degrades_to_queue(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A fully push-disabled clone (the watcher clones) cannot open an MR."""
        monkeypatch.setattr(mr.shutil, "which", lambda _n: "/usr/bin/glab")
        recipe = _recipe(tmp_path, fetch_url="DISABLED_NO_PUSH")
        assert (
            recipe.draft(summary="fix: thing", description="body", diff="d", fingerprint="ff01")
            == "QUEUED:ff01"
        )

    def test_failed_push_degrades_to_queue(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(mr.shutil, "which", lambda _n: "/usr/bin/glab")

        def fake_git(self, *args, timeout=30.0):  # noqa: ANN001
            return subprocess.CompletedProcess(args, 1, "", "remote rejected")

        monkeypatch.setattr(mr.GitLabMRRecipe, "_git", fake_git)
        recipe = _recipe(tmp_path, fetch_url="https://gitlab.com/o/r.git")
        assert (
            recipe.draft(summary="fix: thing", description="body", diff="d", fingerprint="ff02")
            == "QUEUED:ff02"
        )


class TestBranchNaming:
    def test_branch_is_app_namespaced(self, tmp_path: Path) -> None:
        assert _recipe(tmp_path).branch_name(kind="perf", fingerprint="ab12") == (
            "auto-improvement/perf-ab12"
        )

    def test_generated_branch_can_never_be_protected(self, tmp_path: Path) -> None:
        """Same authoritative gate as the GitHub recipe — shared base, shared policy."""
        recipe = _recipe(tmp_path)
        for protected in ("main", "master", "mainline", "release/1.0"):  # wokeignore:rule=master
            branch = recipe.branch_name(kind=protected, fingerprint="aa")
            ok, _ = recipe._authorize(branch)
            assert ok, f"generated branch from {protected!r} was refused: {branch}"


class TestAuthenticatedGitlabRemote:
    def test_https_rewritten_to_ssh_when_glab_prefers_ssh(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(mr, "_glab_prefers_ssh", lambda _h: True)
        assert (
            mr.prefer_authenticated_gitlab_remote("https://gitlab.example.com/group/sub/p.git")
            == "git@gitlab.example.com:group/sub/p.git"
        )

    def test_https_kept_when_glab_prefers_https(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(mr, "_glab_prefers_ssh", lambda _h: False)
        url = "https://gitlab.com/o/r.git"
        assert mr.prefer_authenticated_gitlab_remote(url) == url

    def test_github_hosts_are_never_rewritten_here(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The github.com transport decision belongs to the GitHub recipe."""
        monkeypatch.setattr(mr, "_glab_prefers_ssh", lambda _h: True)
        for url in ("https://github.com/o/r.git", "git@gitlab.com:o/r.git", "nonsense"):
            assert mr.prefer_authenticated_gitlab_remote(url) == url

    def test_host_scoped_setting_wins_over_the_global_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same inversion hazard as ``gh``: the per-host value must be read first."""
        calls: list[list[str]] = []

        def fake_run(args, **kw):  # noqa: ANN001
            calls.append(list(args))
            host_scoped = "--host" in args
            return subprocess.CompletedProcess(args, 0, "ssh\n" if host_scoped else "https\n", "")

        monkeypatch.setattr(mr.shutil, "which", lambda _n: "/usr/bin/glab")
        monkeypatch.setattr(mr.subprocess, "run", fake_run)
        assert mr._glab_prefers_ssh("gitlab.example.com") is True
        assert "--host" in calls[0], "the host-scoped lookup must be tried first"


class TestProviderSelection:
    """The factory seam routes.py and the profile build through."""

    def test_provider_of_infers_from_the_host(self) -> None:
        assert _provider_of("https://github.com/o/r") == "github"
        assert _provider_of("https://www.github.com/o/r") == "github"
        assert _provider_of("https://gitlab.com/o/r") == "gitlab"
        # An unrecognized host (not SaaS, not in the operator's self-managed
        # allowlist) fails toward GitHub: this fallback only serves configs written
        # before the persisted ``provider`` key, and those could only be GitHub.
        assert _provider_of("https://gitlab.example.com/group/sub/p") == "github"
        assert _provider_of("") == "github"
        assert _provider_of("not a url") == "github"

    def test_a_stored_provider_wins_over_the_url(self) -> None:
        cfg = {"provider": "gitlab", "target_url": "https://github.com/o/r"}
        assert target_provider(cfg) == "gitlab"
        assert target_provider({"target_url": "https://gitlab.com/o/r"}) == "gitlab"
        assert target_provider({}) == "github"

    def test_factory_builds_the_gitlab_recipe_for_a_gitlab_target(self, tmp_path: Path) -> None:
        recipe = build_pr_recipe(
            {"target_url": "https://gitlab.com/group/proj"},
            clone_path=tmp_path / "c",
            pr_queue_dir=tmp_path / "q",
            base_ref="origin/main",
        )
        assert isinstance(recipe, mr.GitLabMRRecipe)
        assert isinstance(recipe, PRRecipe)

    def test_factory_builds_the_github_recipe_by_default(self, tmp_path: Path) -> None:
        from kiro_crew.apps.builtins.auto_improvement.profiles.github_repo.pr_recipe import (
            GitHubPRRecipe,
        )

        recipe = build_pr_recipe(
            {"target_url": "https://github.com/o/r", "githubUser": "zedmor"},
            clone_path=tmp_path / "c",
            pr_queue_dir=tmp_path / "q",
            base_ref="origin/main",
        )
        assert isinstance(recipe, GitHubPRRecipe)
        assert recipe.namespace == "github/zedmor"

    def test_dispatching_remote_rewrite_picks_the_hosts_provider(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``commit.py`` calls this with no provider knowledge of its own."""
        from kiro_crew.apps.builtins.auto_improvement.profiles.github_repo import (
            pr_recipe as gh_mod,
        )

        monkeypatch.setattr(gh_mod, "_gh_prefers_ssh", lambda: True)
        monkeypatch.setattr(mr, "_glab_prefers_ssh", lambda _h: True)
        assert prefer_authenticated_remote("https://github.com/o/r.git") == "git@github.com:o/r.git"
        assert (
            prefer_authenticated_remote("https://gitlab.example.com/g/s/p.git")
            == "git@gitlab.example.com:g/s/p.git"
        )
        # Hostless / SSH-form strings pass through untouched.
        assert prefer_authenticated_remote("git@gitlab.com:o/r.git") == "git@gitlab.com:o/r.git"
        assert prefer_authenticated_remote("") == ""


class TestProfileSelectsTheRecipe:
    def test_a_gitlab_target_gets_the_mr_recipe_on_field_five(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One profile serves both hosts; only field ⑤ changes with the provider."""
        monkeypatch.setattr(
            "kiro_crew.apps.builtins.auto_improvement.backend.store.data_dir",
            lambda: tmp_path / "data",
        )
        monkeypatch.setattr(
            "kiro_crew.apps.builtins.auto_improvement.backend.store.workspace_dir",
            lambda: tmp_path / "data",
        )
        from kiro_crew.apps.builtins.auto_improvement.profiles import build_profile

        clone = tmp_path / "clone"
        clone.mkdir()
        prof = build_profile(
            {
                "clone": str(clone),
                "branch": "main",
                "target_url": "https://gitlab.com/group/proj",
            }
        )
        assert isinstance(prof.pr_recipe, mr.GitLabMRRecipe)
