"""Code-channel workspace: per-channel worktree, Code-tab diff, context bar, PR/CI, cleanup.

Real git against throwaway repositories; Slack, the dashboard session and ``gh``
are fakes recorded per test. ``sandboxed_spawn_argv`` is a passthrough so the
git these modules run is exercised on every host (the sandbox itself has its own
tests); the isolation contract is asserted separately below.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew.dashboard.handlers import worktree as wt
from kiro_crew.slack import features
from kiro_crew.slack.code_channels import binding as cc_binding
from kiro_crew.slack.code_channels.models import CodeChannelApiError
from kiro_crew.slack.code_channels.workspace import diff as diff_mod
from kiro_crew.slack.code_channels.workspace import git_ops, service
from kiro_crew.slack.code_channels.workspace.context_bar import ICONS, BarState, build_items
from kiro_crew.slack.code_channels.workspace.diff import (
    MAX_VIEW_BYTES,
    OMITTED,
    assemble,
    render_untracked,
)
from kiro_crew.slack.code_channels.workspace.pr import PrLookup, PullRequest
from kiro_crew.slack.code_channels.workspace.remote import RepoRef, parse_remote
from kiro_crew.slack.code_channels.workspace.store import WorkspaceStore


def _passthrough_spawn(argv, mode="standard", **kw):
    return list(argv), dict(os.environ), None


@pytest.fixture(autouse=True)
def _no_sandbox(_floor_monkeypatch):
    _floor_monkeypatch.setattr(wt, "sandboxed_spawn_argv", _passthrough_spawn)
    _floor_monkeypatch.setattr(git_ops, "sandboxed_spawn_argv", _passthrough_spawn)


@pytest.fixture(autouse=True)
def _feature_on(_floor_monkeypatch):
    """Toggle on and code_channels:manage granted, unless a test says otherwise."""
    features.reset_capabilities()
    features.record_granted_scopes(["chat:write", "code_channels:manage"])
    _floor_monkeypatch.setattr(cc_binding, "live_config", lambda orch=None: _cfg(True))
    yield
    features.reset_capabilities()


def _cfg(on: bool):
    return SimpleNamespace(slack=SimpleNamespace(code_channels=on))


#: Ceiling for every self-unblocked wait in this file: a wait that cannot end
#: fails at its own synchronization point instead of hanging the worker.
_WAIT = 30.0


async def _until(predicate, timeout: float = _WAIT) -> None:
    """Yield to the loop until ``predicate()`` holds, failing after ``timeout``."""

    async def _poll():
        while not predicate():
            await asyncio.sleep(0)

    await asyncio.wait_for(_poll(), timeout=timeout)


@pytest.fixture
def audits(monkeypatch):
    rows: list[tuple[str, str, str, str]] = []
    monkeypatch.setattr(
        service, "_audit", lambda op, outcome, res, error="": rows.append((op, outcome, res, error))
    )
    return rows


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True, encoding="utf-8"
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path) -> Path:
    """A repo on ``main`` with an ``origin`` remote, so ``origin/HEAD`` resolves."""
    remote = tmp_path / "remote.git"
    _git("init", "-q", "--bare", "-b", "main", str(remote), cwd=tmp_path)
    root = tmp_path / "proj"
    root.mkdir()
    _git("init", "-q", "-b", "main", cwd=root)
    for key, value in (("user.email", "t@example.com"), ("user.name", "T"), ("gc.auto", "0")):
        _git("config", key, value, cwd=root)
    (root / "README.md").write_text("hello\n")
    (root / "pkg").mkdir()
    (root / "pkg" / "mod.py").write_text("x = 1\n")
    _git("add", ".", cwd=root)
    _git("commit", "-q", "-m", "init", cwd=root)
    _git("remote", "add", "origin", "https://user:s3cret@github.com/acme/widgets.git", cwd=root)
    # Push through a local URL, then keep the https form as the configured URL.
    _git("push", "-q", str(remote), "main:main", cwd=root)
    _git("fetch", "-q", str(remote), "+refs/heads/*:refs/remotes/origin/*", cwd=root)
    _git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main", cwd=root)
    return root


@dataclass
class FakeBinding:
    channel_id: str
    team_id: str = "T1"
    session_key: str = "chat-1"
    project_dir: str | None = None
    title: str = "Fix the widget"


class FakeClient:
    """Stands in for the core's ``CodeChannelsClient`` (the three workspace writes).

    ``fail[method]`` is a Slack error code the call answers with, raised the way
    the real client raises it (``CodeChannelApiError``), and handed to the
    feature's latch the way the real client's ``_call`` does.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.fail: dict[str, str] = {}

    async def _record(self, method: str, channel_id: str, **kwargs):
        self.calls.append((method, channel_id, kwargs))
        code = self.fail.get(method)
        if code:
            features.latch_unavailable(features.CODE_CHANNELS, code)
            raise CodeChannelApiError(f"agents.conversations.{method}", code)
        return "V1" if method == "set_diff_view" else None

    async def set_context_bar(self, channel_id, items):
        return await self._record("set_context_bar", channel_id, context_bar_items=items)

    async def set_diff_view(self, channel_id, content, *, base_branch="", head_branch=""):
        return await self._record(
            "set_diff_view",
            channel_id,
            content=content,
            base_branch=base_branch,
            head_branch=head_branch,
        )

    async def remove_view(self, channel_id, view_id):
        return await self._record("remove_view", channel_id, view_id=view_id)

    def of(self, method: str) -> list[dict]:
        return [kw for m, _c, kw in self.calls if m == method]


class Harness:
    def __init__(self, tmp_path: Path, pr: PrLookup | None = None) -> None:
        self.client = FakeClient()
        self.projects: list[tuple[str, str]] = []
        self.pr = pr or PrLookup("none")
        self.pr_queries: list[tuple[RepoRef, str]] = []
        self.store = WorkspaceStore(tmp_path / "store.json")

        async def set_project(key: str, path: str) -> bool:
            self.projects.append((key, path))
            return True

        async def find_pr(repo: RepoRef, branch: str) -> PrLookup:
            self.pr_queries.append((repo, branch))
            return self.pr

        self.svc = service.WorkspaceService(
            lambda: self.client, store=self.store, set_project=set_project, find_pr=find_pr
        )

    def run(self, coro):
        return asyncio.run(coro)


def _bar(client: FakeClient) -> list[dict]:
    return client.of("set_context_bar")[-1]["context_bar_items"]


# ── bind ────────────────────────────────────────────────────────────────────


def test_folder_with_no_project_makes_no_worktree_and_no_call(tmp_path, audits):
    h = Harness(tmp_path)
    rec = h.run(h.svc.ensure(FakeBinding("C1", project_dir=None)))
    h.run(h.svc.refresh(FakeBinding("C1", project_dir=None)))
    assert rec is not None and rec.worktree == "" and rec.worktree_skipped == "no_project"
    assert h.projects == [] and h.client.calls == []


def test_non_git_project_shows_its_folder_without_a_worktree(tmp_path, audits):
    folder = tmp_path / "notes"
    folder.mkdir()
    (folder / "a.txt").write_text("x\n")
    h = Harness(tmp_path)
    rec = h.run(h.svc.ensure(FakeBinding("C1", project_dir=str(folder))))
    h.run(h.svc.refresh(FakeBinding("C1", project_dir=str(folder))))
    assert rec.worktree_skipped == "not_a_repo" and rec.worktree == ""
    assert h.projects == []
    assert _bar(h.client) == [{"key": "folder", "label": "notes", "icon": "folder"}]
    assert h.client.of("set_diff_view") == []


def test_git_project_gets_a_fresh_worktree_off_main_despite_a_dirty_checkout(
    tmp_path, repo, audits
):
    (repo / "README.md").write_text("dirty, uncommitted\n")
    _git("checkout", "-q", "-b", "elsewhere", cwd=repo)
    h = Harness(tmp_path)
    rec = h.run(h.svc.ensure(FakeBinding("C1", project_dir=str(repo))))
    wt_path = Path(rec.worktree)
    assert wt_path.is_dir() and wt_path != repo
    assert rec.branch.startswith("slack/fix-the-widget-")
    assert _git("symbolic-ref", "--short", "HEAD", cwd=wt_path) == rec.branch
    assert rec.base_sha == _git("rev-parse", "origin/main", cwd=repo)
    # The worktree is cut from the default branch, not the dirty checkout.
    assert (wt_path / "README.md").read_text() == "hello\n"
    assert (repo / "README.md").read_text() == "dirty, uncommitted\n"
    assert _git("symbolic-ref", "--short", "HEAD", cwd=repo) == "elsewhere"
    assert h.projects == [("chat-1", str(wt_path))]
    assert _bar(h.client) == [
        {
            "key": "repo",
            "label": "acme/widgets",
            "icon": "folder",
            "url": "https://github.com/acme/widgets",
        },
        {"key": "branch", "label": rec.branch, "icon": "branch"},
    ]
    assert any(a[0] == "code_channel_worktree" and a[1] == "allowed" for a in audits)
    # The remote's credential never reaches Slack.
    assert "s3cret" not in repr(h.client.calls)


def test_a_project_subdirectory_points_the_session_at_the_same_subdirectory(tmp_path, repo, audits):
    h = Harness(tmp_path)
    rec = h.run(h.svc.ensure(FakeBinding("C1", project_dir=str(repo / "pkg"))))
    assert rec.subdir == "pkg"
    assert h.projects == [("chat-1", os.path.join(rec.worktree, "pkg"))]


def test_a_binding_without_a_project_isolates_the_sessions_default_git_project(
    tmp_path, repo, audits
):
    """No folder project: the session still runs in the dashboard default; isolate it."""
    h = Harness(tmp_path)

    async def project_of(key):
        return str(repo)

    h.svc._project_of = project_of
    a = h.run(h.svc.ensure(FakeBinding("C1", project_dir=None)))
    b = h.run(h.svc.ensure(FakeBinding("C2", project_dir=None)))
    assert a.worktree and b.worktree and a.worktree != b.worktree
    assert a.project_dir == "" and a.resolved_project == str(repo)
    # A later turn start does not see a "changed project" and rebind.
    again = h.run(h.svc.ensure(FakeBinding("C1", project_dir=None)))
    assert again.worktree == a.worktree


def test_a_replacement_session_is_pointed_at_the_existing_worktree_once(tmp_path, repo, audits):
    h = Harness(tmp_path)
    _binding, rec = _bound(h, repo)
    replacement = FakeBinding("C1", project_dir=str(repo), session_key="chat-2")
    for _ in range(3):
        h.run(h.svc.ensure(replacement))
    assert h.projects == [("chat-1", rec.worktree), ("chat-2", rec.worktree)]
    assert h.store.get("T1", "C1").worktree == rec.worktree  # no second worktree


def test_a_rewritten_record_never_steers_a_read_or_a_removal(tmp_path, repo, audits):
    """The record is a file; git must confirm it names this channel's own worktree."""
    h = Harness(tmp_path)
    binding, rec = _bound(h, repo)
    elsewhere = tmp_path / "elsewhere"
    # A clone carries the base commit, so only the record check stands between
    # this tree's untracked files and the channel.
    _git("clone", "-q", str(repo), str(elsewhere), cwd=tmp_path)
    (elsewhere / ".netrc").write_text("machine x login u password hunter2\n")
    forged = h.store.get("T1", "C1")
    forged.worktree, forged.repo_root = str(elsewhere), str(elsewhere)
    h.store.put(forged)
    h.run(h.svc.refresh(binding))
    assert "hunter2" not in repr(h.client.calls) and h.client.of("set_diff_view") == []
    # Archive: a CLEAN linked worktree someone else made, which the
    # keep-if-dirty rule alone would remove, is never removed on the record's word.
    theirs = tmp_path / "theirs"
    _git("worktree", "add", "-q", "-b", "user-feature", str(theirs), cwd=repo)
    forged = h.store.get("T1", "C1")
    forged.worktree, forged.repo_root = str(theirs), str(repo)
    h.store.put(forged)
    assert h.run(h.svc.release("T1", "C1")) == "kept"
    assert theirs.is_dir() and _git("branch", "--list", "user-feature", cwd=repo)
    assert Path(rec.worktree).is_dir()
    assert ("code_channel_worktree", "denied") in [(a[0], a[1]) for a in audits]


def test_a_sensitive_untracked_path_is_never_read(tmp_path, repo, audits, monkeypatch):
    h = Harness(tmp_path)
    binding, rec = _bound(h, repo)
    tree = Path(rec.worktree)
    (tree / "README.md").write_text("visible change\n")
    (tree / "id_ed25519").write_text("PRIVATE KEY MATERIAL\n")
    real = service.is_sensitive_path
    monkeypatch.setattr(service, "is_sensitive_path", lambda p: p.endswith("id_ed25519") or real(p))
    h.run(h.svc.refresh(binding))
    body = h.client.of("set_diff_view")[-1]["content"]
    assert "PRIVATE KEY" not in body and body.startswith("Diff truncated")


def test_two_channels_on_one_repo_get_distinct_worktrees_and_branches(tmp_path, repo, audits):
    h = Harness(tmp_path)
    a = h.run(h.svc.ensure(FakeBinding("C1", project_dir=str(repo))))
    b = h.run(h.svc.ensure(FakeBinding("C2", project_dir=str(repo))))
    assert a.branch != b.branch and a.worktree != b.worktree
    assert Path(a.worktree).is_dir() and Path(b.worktree).is_dir()


def test_rebinding_is_idempotent_and_a_lost_record_adopts_the_same_worktree(tmp_path, repo, audits):
    h = Harness(tmp_path)
    first = h.run(h.svc.ensure(FakeBinding("C1", project_dir=str(repo))))
    (Path(first.worktree) / "new.txt").write_text("work in progress\n")
    again = h.run(h.svc.ensure(FakeBinding("C1", project_dir=str(repo))))
    assert again.worktree == first.worktree
    assert len(h.client.of("set_context_bar")) == 1  # turn start on a known channel: no call
    h.store.delete("T1", "C1")
    adopted = h.run(h.svc.ensure(FakeBinding("C1", project_dir=str(repo))))
    assert adopted.worktree == first.worktree and adopted.base_sha == first.base_sha
    assert (Path(adopted.worktree) / "new.txt").exists()


def test_a_host_without_a_sandbox_degrades_to_no_worktree(tmp_path, repo, audits, monkeypatch):
    def refuse(*a, **k):
        raise RuntimeError("no sandbox backend")

    monkeypatch.setattr(wt, "sandboxed_spawn_argv", refuse)
    monkeypatch.setattr(git_ops, "sandboxed_spawn_argv", refuse)
    h = Harness(tmp_path)
    rec = h.run(h.svc.ensure(FakeBinding("C1", project_dir=str(repo))))
    assert rec.worktree == "" and rec.worktree_skipped == "sandbox_unavailable"
    assert h.projects == []
    assert any(a[:2] == ("code_channel_worktree", "denied") for a in audits)


def test_a_repo_declaring_a_checkout_filter_gets_no_worktree(tmp_path, repo, audits):
    _git("config", "filter.evil.smudge", "touch /tmp/pwned", cwd=repo)
    h = Harness(tmp_path)
    rec = h.run(h.svc.ensure(FakeBinding("C1", project_dir=str(repo))))
    assert rec.worktree == "" and "content filter" in rec.worktree_skipped
    assert any(a[:2] == ("code_channel_worktree", "error") for a in audits)


# ── diff ────────────────────────────────────────────────────────────────────


def _bound(h: Harness, repo: Path, channel: str = "C1"):
    binding = FakeBinding(channel, project_dir=str(repo))
    return binding, h.run(h.svc.ensure(binding))


def test_a_turn_that_edits_files_publishes_committed_staged_unstaged_and_new_files(
    tmp_path, repo, audits
):
    h = Harness(tmp_path)
    binding, rec = _bound(h, repo)
    tree = Path(rec.worktree)
    (tree / "pkg" / "mod.py").write_text("x = 2\n")
    _git("commit", "-q", "-am", "bump", cwd=tree)
    (tree / "README.md").write_text("hello\nworld\n")  # unstaged
    (tree / "added.py").write_text("print('hi')")  # untracked, no final newline
    (tree / "blob.bin").write_bytes(b"\x00\x01\x02binary")
    h.run(h.svc.refresh(binding))
    (view,) = h.client.of("set_diff_view")
    assert view["base_branch"] == "main" and view["head_branch"] == rec.branch
    body = view["content"]
    assert "-x = 1\n+x = 2" in body
    assert "+world" in body
    assert "diff --git a/added.py b/added.py\nnew file mode 100644" in body
    assert "+print('hi')\n\\ No newline at end of file" in body
    assert "Binary files /dev/null and b/blob.bin differ" in body
    assert "truncated" not in body


def test_an_unchanged_turn_republishes_nothing(tmp_path, repo, audits):
    h = Harness(tmp_path)
    binding, rec = _bound(h, repo)
    (Path(rec.worktree) / "README.md").write_text("changed\n")
    h.run(h.svc.refresh(binding))
    before = list(h.client.calls)
    h.run(h.svc.refresh(binding))
    assert h.client.calls == before


def test_an_empty_diff_publishes_no_view(tmp_path, repo, audits):
    h = Harness(tmp_path)
    binding, _rec = _bound(h, repo)
    h.run(h.svc.refresh(binding))
    assert h.client.of("set_diff_view") == []


def test_reverting_every_change_removes_the_published_diff_tab(tmp_path, repo, audits):
    h = Harness(tmp_path)
    binding, rec = _bound(h, repo)
    readme = Path(rec.worktree) / "README.md"
    readme.write_text("changed\n")
    h.run(h.svc.refresh(binding))
    readme.write_text("hello\n")
    h.run(h.svc.refresh(binding))
    assert h.client.of("remove_view") == [{"view_id": "V1"}]


def test_an_oversized_diff_is_cut_under_the_limit_with_a_note(tmp_path, repo, audits):
    h = Harness(tmp_path)
    binding, rec = _bound(h, repo)
    tree = Path(rec.worktree)
    for i in range(3):
        (tree / f"big{i}.txt").write_text("".join(f"line {n} {'x' * 60}\n" for n in range(9000)))
    _git("add", ".", cwd=tree)
    h.run(h.svc.refresh(binding))
    body = h.client.of("set_diff_view")[-1]["content"]
    assert len(body.encode()) <= MAX_VIEW_BYTES
    assert body.startswith("Diff truncated to fit Slack's 1,000,000-byte view limit")


def test_a_tracked_binary_change_renders_as_git_does(tmp_path, repo, audits):
    h = Harness(tmp_path)
    binding, rec = _bound(h, repo)
    tree = Path(rec.worktree)
    (tree / "img.png").write_bytes(b"\x89PNG\x00\x00data")
    _git("add", "img.png", cwd=tree)
    h.run(h.svc.refresh(binding))
    assert (
        "Binary files /dev/null and b/img.png differ" in h.client.of("set_diff_view")[-1]["content"]
    )


def test_an_untracked_symlink_is_shown_as_a_link_never_followed(tmp_path, repo, audits):
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP-SECRET-VALUE\n")
    h = Harness(tmp_path)
    binding, rec = _bound(h, repo)
    os.symlink(secret, Path(rec.worktree) / "leak.txt")
    h.run(h.svc.refresh(binding))
    body = h.client.of("set_diff_view")[-1]["content"]
    assert "new file mode 120000" in body and "TOP-SECRET-VALUE" not in body


# ── PR / CI ─────────────────────────────────────────────────────────────────

_PR_URL = "https://github.com/acme/widgets/pull/42"


def test_an_open_pr_and_its_ci_join_the_context_bar(tmp_path, repo, audits):
    h = Harness(tmp_path, PrLookup("found", PullRequest(42, _PR_URL, "open", "passed")))
    binding, rec = _bound(h, repo)
    h.run(h.svc.refresh(binding))
    keys = {i["key"]: i for i in _bar(h.client)}
    assert keys["pr"] == {
        "key": "pr",
        "label": "PR #42 is open",
        "icon": "hierarchy",
        "url": _PR_URL,
    }
    assert keys["ci"] == {
        "key": "ci",
        "label": "Tests passing",
        "icon": "terminal",
        "url": _PR_URL + "/checks",
    }
    repo_ref, branch = h.pr_queries[-1]
    assert repo_ref == RepoRef("github.com", "acme", "widgets") and branch == rec.branch


@pytest.mark.parametrize("state,label", [("merged", "PR #42 merged"), ("closed", "PR #42 closed")])
def test_a_finished_pr_shows_its_state_and_no_ci(tmp_path, repo, audits, state, label):
    h = Harness(tmp_path, PrLookup("found", PullRequest(42, _PR_URL, state, "failed")))
    binding, _rec = _bound(h, repo)
    h.run(h.svc.refresh(binding))
    keys = {i["key"]: i for i in _bar(h.client)}
    assert keys["pr"]["label"] == label and "ci" not in keys


def test_gh_signed_out_keeps_the_last_known_pr_and_drops_its_ci(tmp_path, repo, audits):
    h = Harness(tmp_path, PrLookup("found", PullRequest(42, _PR_URL, "open", "running")))
    binding, _rec = _bound(h, repo)
    h.run(h.svc.refresh(binding))
    h.pr = PrLookup("unavailable", reason="auth")
    (Path(h.store.get("T1", "C1").worktree) / "README.md").write_text("more\n")
    h.run(h.svc.refresh(binding))
    keys = {i["key"] for i in _bar(h.client)}
    assert "pr" in keys and "ci" not in keys


def test_find_pull_request_reads_through_the_provider_runner(monkeypatch):
    from kiro_crew.dashboard.source_providers import chip_refresh, runner
    from kiro_crew.dashboard.source_providers.contract import SourceProviderError
    from kiro_crew.slack.code_channels.workspace import pr as pr_mod

    calls = []

    async def fake_run_json(*argv, host=""):
        calls.append((argv, host))
        return [
            {
                "number": 7,
                "url": "https://github.com/acme/widgets/pull/7",
                "state": "CLOSED",
                "isDraft": False,
            },
            {
                "number": 9,
                "url": "https://github.com/acme/widgets/pull/9",
                "state": "OPEN",
                "isDraft": True,
            },
            {"number": 3, "url": "https://evil.example/acme/widgets/pull/3", "state": "OPEN"},
        ]

    async def fake_status(url):
        return {"state": "draft", "ci": "failed"}

    monkeypatch.setattr(runner, "_run_json", fake_run_json)
    monkeypatch.setattr(chip_refresh, "_fetch_check_status", fake_status)
    repo = RepoRef("github.com", "acme", "widgets")
    got = asyncio.run(pr_mod.find_pull_request(repo, "slack/x-1"))
    assert got.status == "found" and got.pr == PullRequest(
        9, "https://github.com/acme/widgets/pull/9", "draft", "failed"
    )
    argv, host = calls[0]
    assert argv[:3] == ("gh", "pr", "list") and "--repo" in argv and "acme/widgets" in argv
    assert host == "github.com"

    async def signed_out(*argv, host=""):
        raise SourceProviderError("gh: not logged in. Run `gh auth login`, then retry.")

    monkeypatch.setattr(runner, "_run_json", signed_out)
    assert asyncio.run(pr_mod.find_pull_request(repo, "slack/x-1")).status == "unavailable"

    async def missing(*argv, host=""):
        raise SourceProviderError("gh is not installed", reason="executable_untrusted")

    monkeypatch.setattr(runner, "_run_json", missing)
    assert asyncio.run(pr_mod.find_pull_request(repo, "slack/x-1")).status == "unavailable"

    async def empty(*argv, host=""):
        return []

    monkeypatch.setattr(runner, "_run_json", empty)
    assert asyncio.run(pr_mod.find_pull_request(repo, "slack/x-1")).status == "none"
    # A branch git would read as a flag never reaches gh; neither does an unlisted host.
    assert asyncio.run(pr_mod.find_pull_request(repo, "-rf")).status == "unavailable"
    from kiro_crew.dashboard.source_providers import hosts

    loads: list[int] = []
    allowed: set[str] = set()

    async def load():
        loads.append(1)
        allowed.add("ghe.internal")
        return frozenset()

    monkeypatch.setattr(hosts, "ensure_gitlab_hosts_loaded", load)
    monkeypatch.setattr(hosts, "_allowed_github_hosts", lambda: frozenset(allowed))
    ghe = RepoRef("ghe.internal", "acme", "widgets")
    other = RepoRef("ghe.other", "acme", "widgets")
    monkeypatch.setattr(runner, "_run_json", empty)
    # The Enterprise allowlist is loaded before it is read (it is empty while cold).
    assert asyncio.run(pr_mod.find_pull_request(ghe, "slack/x-1")).status == "none"
    assert asyncio.run(pr_mod.find_pull_request(other, "slack/x-1")).reason == "host"
    assert len(loads) == 2


# ── Slack refusals ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("code", ["not_agent_channel", "not_authorized", "channel_not_found"])
def test_a_channel_specific_slack_error_turns_publishing_off_for_that_channel(
    tmp_path, repo, audits, code
):
    h = Harness(tmp_path)
    h.client.fail["set_context_bar"] = code
    binding, rec = _bound(h, repo)
    assert rec.worktree  # the worktree still works without Slack's views
    (Path(rec.worktree) / "README.md").write_text("changed\n")
    h.run(h.svc.refresh(binding))
    assert [m for m, _c, _k in h.client.calls] == ["set_context_bar"]
    assert h.store.get("T1", "C1").slack_off == code
    assert ("code_channel_publish", "denied") in [(a[0], a[1]) for a in audits]
    assert features.code_channels_available()  # one channel's refusal latches nothing


@pytest.mark.parametrize("code", ["feature_disabled", "missing_scope", "not_allowed_token_type"])
def test_an_install_wide_answer_is_not_persisted_on_the_channel(tmp_path, repo, audits, code):
    """It goes to the process latch, which a reconnect lifts; the record must not outlive it."""
    h = Harness(tmp_path)
    h.client.fail["set_context_bar"] = code
    _bound(h, repo)
    assert h.store.get("T1", "C1").slack_off == ""
    assert not features.code_channels_available()


def test_a_transient_slack_error_is_retried_next_turn(tmp_path, repo, audits):
    h = Harness(tmp_path)
    h.client.fail["set_diff_view"] = "ratelimited"
    binding, rec = _bound(h, repo)
    (Path(rec.worktree) / "README.md").write_text("changed\n")
    h.run(h.svc.refresh(binding))
    h.client.fail.clear()
    h.run(h.svc.refresh(binding))
    assert len(h.client.of("set_diff_view")) == 2


def test_a_refusal_raised_before_sending_is_logged_and_retried(tmp_path, repo, audits):
    """A request the core's client refuses locally (bad id, oversized diff) changes nothing."""
    from kiro_crew.slack.code_channels.models import CodeChannelRequestError

    h = Harness(tmp_path)

    async def refuse(*_a, **_k):
        raise CodeChannelRequestError("diff exceeds 1000000 bytes")

    h.client.set_context_bar = refuse
    binding, _rec = _bound(h, repo)
    assert h.store.get("T1", "C1").slack_off == "" and features.code_channels_available()


def test_no_slack_client_yet_publishes_nothing_and_retries(tmp_path, repo, audits):
    h = Harness(tmp_path)
    h.svc._client = lambda: None  # the gateway has not connected to Slack yet
    binding, rec = _bound(h, repo)
    assert rec.worktree and h.store.get("T1", "C1").bar_hash == ""
    h.svc._client = lambda: h.client
    (Path(rec.worktree) / "README.md").write_text("changed\n")
    h.run(h.svc.refresh(binding))
    assert h.client.of("set_context_bar") and h.client.of("set_diff_view")


# ── toggle / registration ───────────────────────────────────────────────────


def test_register_is_the_only_entry_point_and_attaches_the_turn_hooks(tmp_path):
    class Registry:
        def __init__(self):
            self.start, self.end = [], []

        def on_turn_start(self, hook):
            self.start.append(hook)

        def on_turn_end(self, hook):
            self.end.append(hook)

    reg = Registry()
    h = Harness(tmp_path)
    service.register(reg, h.svc)
    assert reg.start == [h.svc.on_turn_start] and reg.end == [h.svc.on_turn_end]
    assert h.client.calls == []


def test_turn_end_returns_at_once_and_coalesces_into_one_rerun(tmp_path, repo, audits, monkeypatch):
    h = Harness(tmp_path)
    binding, rec = _bound(h, repo)
    (Path(rec.worktree) / "README.md").write_text("changed\n")
    runs: list[int] = []
    real_refresh = h.svc.refresh

    async def scenario():
        gate = asyncio.Event()

        async def counted(b):
            runs.append(1)
            if len(runs) == 1:
                await asyncio.wait_for(gate.wait(), timeout=_WAIT)
            await real_refresh(b)

        monkeypatch.setattr(h.svc, "refresh", counted)
        await h.svc.on_turn_end(binding, object())
        await _until(lambda: runs)  # the first refresh is now running, parked on the gate
        # Two more turns end while it runs: they fold into ONE rerun.
        await h.svc.on_turn_end(binding, object())
        await h.svc.on_turn_end(binding, object())
        gate.set()
        await asyncio.wait_for(h.svc.drain(), timeout=30)

    h.run(scenario())
    assert len(runs) == 2
    assert len(h.client.of("set_diff_view")) == 1


def test_nothing_outside_code_channels_imports_the_workspace():
    """With the toggle off the workspace is unreachable: only the core registers it."""
    src = Path(service.__file__).resolve().parents[3]
    pkg = src / "slack" / "code_channels"
    offenders = [
        p
        for p in src.rglob("*.py")
        if pkg not in p.parents
        and "code_channels.workspace" in p.read_text(encoding="utf-8", errors="ignore")
    ]
    assert offenders == []


# ── gate: toggle and availability ───────────────────────────────────────────


def _assert_untouched(h: Harness, repo: Path) -> None:
    assert h.client.calls == [] and h.projects == []
    assert _git("worktree", "list", "--porcelain", cwd=repo).count("worktree ") == 1
    assert h.store.get("T1", "C1") is None


def _every_hook(h: Harness, binding: FakeBinding) -> None:
    async def go():
        assert await h.svc.ensure(binding) is None
        await h.svc.on_turn_start(binding, object())
        await h.svc.on_turn_end(binding, object())
        await h.svc.drain()

    h.run(go())


def test_toggle_off_is_identical_to_main(tmp_path, repo, audits, monkeypatch):
    monkeypatch.setattr(cc_binding, "live_config", lambda orch=None: _cfg(False))
    h = Harness(tmp_path)
    _every_hook(h, FakeBinding("C1", project_dir=str(repo)))
    _assert_untouched(h, repo)
    assert audits == []


def test_an_unreadable_config_is_off(tmp_path, repo, audits, monkeypatch):
    monkeypatch.setattr(cc_binding, "live_config", lambda orch=None: None)
    h = Harness(tmp_path)
    _every_hook(h, FakeBinding("C1", project_dir=str(repo)))
    _assert_untouched(h, repo)


@pytest.mark.parametrize("scopes", [None, ["chat:write"]])
def test_toggle_on_but_scope_not_granted_is_identical_to_main(tmp_path, repo, audits, scopes):
    """No scope probe yet, or one without code_channels:manage: off before any git runs."""
    features.reset_capabilities()
    if scopes is not None:
        features.record_granted_scopes(scopes)
    h = Harness(tmp_path)
    _every_hook(h, FakeBinding("C1", project_dir=str(repo)))
    _assert_untouched(h, repo)
    assert audits == []


@pytest.mark.parametrize("code", ["feature_disabled", "missing_scope", "not_allowed_token_type"])
def test_the_first_feature_off_answer_latches_code_channels_off_for_the_process(
    tmp_path, repo, audits, code
):
    h = Harness(tmp_path)
    h.client.fail["set_context_bar"] = code
    assert h.run(h.svc.ensure(FakeBinding("C1", project_dir=str(repo)))) is not None
    assert not features.code_channels_available()
    calls = list(h.client.calls)
    # Every later hook, on any channel, is now a pass-through: no worktree, no Slack call.
    _every_hook(h, FakeBinding("C2", project_dir=str(repo)))
    h.run(h.svc.refresh(FakeBinding("C1", project_dir=str(repo))))
    assert h.client.calls == calls and h.store.get("T1", "C2") is None
    # A reconnect re-probes and clears the latch; the workspace follows the feature.
    features.clear_latch()  # a reconnect re-probes from scratch
    features.record_granted_scopes(["code_channels:manage"])
    assert features.code_channels_available()


def test_a_transient_error_does_not_latch(tmp_path, repo, audits):
    h = Harness(tmp_path)
    h.client.fail["set_context_bar"] = "ratelimited"
    h.run(h.svc.ensure(FakeBinding("C1", project_dir=str(repo))))
    assert features.code_channels_available()


def test_archive_cleanup_still_runs_after_the_latch(tmp_path, repo, audits):
    h = Harness(tmp_path)
    _binding, rec = _bound(h, repo)
    features.latch_unavailable(features.CODE_CHANNELS, "feature_disabled")
    assert not features.code_channels_available()
    assert h.run(h.svc.release("T1", "C1")) == "removed"
    assert not Path(rec.worktree).exists()


# ── one bind per channel ────────────────────────────────────────────────────


def test_a_caller_timing_out_leaves_one_bind_that_the_next_caller_joins(
    tmp_path, repo, audits, monkeypatch
):
    h = Harness(tmp_path)
    binding = FakeBinding("C1", project_dir=str(repo))
    started: list[int] = []
    real_bind = h.svc._bind
    gate = asyncio.Event()

    async def slow_bind(b):
        started.append(1)
        await asyncio.wait_for(gate.wait(), timeout=_WAIT)
        return await real_bind(b)

    monkeypatch.setattr(h.svc, "_bind", slow_bind)

    async def scenario():
        first = asyncio.ensure_future(h.svc.ensure(binding))
        await _until(lambda: started)
        first.cancel()  # the core's hook timeout, or a cancelled turn
        with pytest.raises(asyncio.CancelledError):
            await first
        second = asyncio.ensure_future(h.svc.ensure(binding))
        await asyncio.sleep(0)
        gate.set()
        return await asyncio.wait_for(second, timeout=60)

    rec = h.run(scenario())
    assert len(started) == 1 and rec is not None and Path(rec.worktree).is_dir()


def test_a_git_error_at_bind_is_retried_a_bounded_number_of_times(
    tmp_path, repo, audits, monkeypatch
):
    h = Harness(tmp_path)
    binding = FakeBinding("C1", project_dir=str(repo))
    tries: list[int] = []
    real = git_ops.create_or_adopt_worktree

    def flaky(root, branch):
        tries.append(1)
        return "fatal: Unable to create index.lock" if len(tries) == 1 else real(root, branch)

    monkeypatch.setattr(git_ops, "create_or_adopt_worktree", flaky)
    first = h.run(h.svc.ensure(binding))
    assert first.worktree == "" and "index.lock" in first.worktree_skipped
    second = h.run(h.svc.ensure(binding))
    assert second.worktree and len(tries) == 2

    always: list[int] = []

    def broken(root, branch):
        always.append(1)
        return "fatal: boom"

    monkeypatch.setattr(git_ops, "create_or_adopt_worktree", broken)
    other = FakeBinding("C2", project_dir=str(repo))
    for _ in range(6):
        h.run(h.svc.ensure(other))
    assert len(always) == 3  # three binds in all, then the failure is settled


def test_an_archive_waits_for_a_bind_in_flight(tmp_path, repo, audits, monkeypatch):
    h = Harness(tmp_path)
    binding = FakeBinding("C1", project_dir=str(repo))
    real_bind = h.svc._bind
    started = asyncio.Event()
    gate = asyncio.Event()

    async def slow_bind(b):
        started.set()
        await asyncio.wait_for(gate.wait(), timeout=_WAIT)
        return await real_bind(b)

    monkeypatch.setattr(h.svc, "_bind", slow_bind)

    async def scenario():
        bind = asyncio.ensure_future(h.svc.ensure(binding))
        await asyncio.wait_for(started.wait(), timeout=_WAIT)
        release = asyncio.ensure_future(h.svc.release("T1", "C1"))
        await asyncio.sleep(0)
        gate.set()
        outcome = await asyncio.wait_for(release, timeout=_WAIT)
        await asyncio.wait_for(bind, timeout=_WAIT)
        return outcome

    assert h.run(scenario()) == "removed"
    assert _git("worktree", "list", "--porcelain", cwd=repo).count("worktree ") == 1


# ── wiring into the core ────────────────────────────────────────────────────


def test_the_core_client_sends_the_workspace_writes_and_latches_refusals():
    """The three writes the workspace makes, end to end through the core's client."""
    from kiro_crew.slack.code_channels.client import CodeChannelsClient
    from kiro_crew.slack.code_channels.models import CodeChannelApiError, CodeChannelRequestError

    sent: list[tuple[str, dict]] = []

    async def api_call(method, json=None, **_kw):
        sent.append((method, json))
        if method == "agents.conversations.removeView":
            return {"ok": False, "error": "missing_scope"}
        if method == "agents.conversations.setView":
            return {"ok": True, "view_id": "V9"}
        return {"ok": True}

    client = CodeChannelsClient(api_call)
    items = [{"key": "repo", "label": "a/b", "icon": "folder", "url": "https://github.com/a/b"}]
    asyncio.run(client.set_context_bar("C1", items))
    view_id = asyncio.run(client.set_diff_view("C1", "diff --git a/x b/x\n", base_branch="main"))
    assert sent[0] == (
        "agents.conversations.setProperties",
        {"channel_id": "C1", "code_channel": {"context_bar_items": items}},
    )
    assert sent[1] == (
        "agents.conversations.setView",
        {
            "channel_id": "C1",
            "type": "diff",
            "content": "diff --git a/x b/x\n",
            "base_branch": "main",
        },
    )
    assert view_id == "V9"
    with pytest.raises(CodeChannelApiError) as err:
        asyncio.run(client.remove_view("C1", "V9"))
    assert sent[2] == ("agents.conversations.removeView", {"channel_id": "C1", "view_id": "V9"})
    assert err.value.code == "missing_scope"
    assert not features.code_channels_available()  # the client latched it itself
    with pytest.raises(CodeChannelRequestError):
        asyncio.run(client.set_diff_view("C1", "x" * 1_000_001))
    assert len(sent) == 3  # refused locally, never sent


def test_installing_the_core_subscribes_the_workspace_to_its_turn_hooks(monkeypatch):
    from kiro_crew.slack.code_channels import hooks
    from kiro_crew.slack.code_channels.workspace import core

    monkeypatch.setattr(core, "_SERVICE", None)
    svc = core.install(lambda: None)
    try:
        assert svc.on_turn_start in hooks._START_HOOKS and svc.on_turn_end in hooks._END_HOOKS
        assert core._watch_on_turn in hooks._START_HOOKS
        assert core.install(lambda: None) is svc  # idempotent
    finally:
        hooks.remove_hook(svc.on_turn_start)
        hooks.remove_hook(svc.on_turn_end)
        hooks.remove_hook(core._watch_on_turn)


def test_prepare_finishes_the_bind_and_points_the_session_before_returning(
    tmp_path, repo, audits, monkeypatch
):
    """The core awaits it ahead of the first turn, which captures the session's project."""
    from kiro_crew.slack.code_channels.workspace import core

    h = Harness(tmp_path)
    monkeypatch.setattr(core, "_SERVICE", h.svc)
    h.run(core.prepare(FakeBinding("C1", project_dir=str(repo))))
    rec = h.store.get("T1", "C1")
    assert Path(rec.worktree).is_dir() and rec.pointed_session == "chat-1"
    assert h.projects == [("chat-1", rec.worktree)]
    monkeypatch.setattr(core, "_SERVICE", None)
    h.run(core.prepare(FakeBinding("C9")))  # no service: a no-op


def test_prepare_never_raises_into_the_bind(tmp_path, repo, audits, monkeypatch):
    from kiro_crew.slack.code_channels.workspace import core

    h = Harness(tmp_path)

    async def boom(_b):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(h.svc, "ensure", boom)
    monkeypatch.setattr(core, "_SERVICE", h.svc)
    h.run(core.prepare(FakeBinding("C1", project_dir=str(repo))))


def test_the_session_is_pointed_once_and_a_later_user_pick_holds(tmp_path, repo, audits):
    h = Harness(tmp_path)
    binding, rec = _bound(h, repo)
    assert h.projects == [("chat-1", rec.worktree)]
    # The user moves the session elsewhere; later turn starts must not move it back.
    for _ in range(3):
        h.run(h.svc.on_turn_start(binding, object()))
    assert h.projects == [("chat-1", rec.worktree)]


# ── conductor-led channels: the channel folder and its workers ──────────────


class FakeDashboard:
    """The two dashboard tables the workspace reads: folders and live slots."""

    def __init__(self) -> None:
        self._folders: list[dict] = [{"id": "F0", "name": "Code channels", "parent_id": ""}]
        self._slots: dict[str, SimpleNamespace] = {}
        self.pushes = 0

    def push_slots_update(self) -> None:
        self.pushes += 1

    def add_folder(self, fid: str, name: str, parent: str, project: str = "") -> str:
        row = {"id": fid, "name": name, "parent_id": parent}
        if project:
            row["project_dir"] = project
        self._folders.append(row)
        return fid

    def add_slot(self, key: str, folder_id: str, *, running: bool = False) -> SimpleNamespace:
        slot = SimpleNamespace(folder_id=folder_id, turn_running=running, project="")
        self._slots[key] = slot
        return slot


def _conductor_harness(tmp_path, monkeypatch):
    h = Harness(tmp_path)
    dash = FakeDashboard()
    made: list[tuple[str, str, str]] = []

    async def create_folder(state, parent_id, name, project_dir):
        made.append((parent_id, name, project_dir))
        return state.add_folder(f"F{len(state._folders)}", name, parent_id, project_dir)

    h.svc._create_folder = create_folder
    statuses: list[tuple[str, str]] = []

    async def set_status(channel_id, status):
        statuses.append((channel_id, status))
        return True

    monkeypatch.setattr(service, "_set_status", set_status)
    return h, dash, made, statuses


def test_a_conductor_led_channel_shows_its_workers_commit_in_the_diff_tab(
    tmp_path, repo, audits, monkeypatch
):
    """Bind -> channel folder (project = worktree) -> conductor + worker filed under it ->
    the worker commits in the worktree during ITS turn -> the diff tab shows it."""
    h, dash, made, statuses = _conductor_harness(tmp_path, monkeypatch)
    planned = h.run(
        h.svc.plan_session(
            dash,
            channel_id="C1",
            team_id="T1",
            title="Fix the widget",
            channel_name="fix-widget",
            project_dir=str(repo),
            parent_folder_id="F0",
        )
    )
    assert planned is not None
    folder_id, worktree = planned
    rec = h.store.get("T1", "C1")
    assert made == [("F0", "fix-widget", rec.worktree)] and worktree == rec.worktree
    assert rec.folder_id == folder_id
    # The session the core then creates in that folder, and the conductor's
    # worker it files under <folder>/kirocrew-worker (inheriting the project).
    dash.add_slot("chat-conductor", folder_id)
    worker_folder = dash.add_folder("FW", "kirocrew-worker", folder_id)
    worker = dash.add_slot("chat-worker", worker_folder, running=True)
    binding = FakeBinding("C1", project_dir=worktree, session_key="chat-conductor")
    h.run(h.svc.ensure(binding))  # the core's prepare() after creating the session
    # Born in the worktree: the only "move" is to where it already is, which the
    # dashboard's setter answers as a no-op (no reset armed).
    assert h.projects == [("chat-conductor", worktree)]

    async def scenario():
        await h.svc.watch_tick(dash)  # worker running, conductor idle
        assert statuses == [("C1", "processing")]
        tree = Path(worktree)
        (tree / "README.md").write_text("hello\nworker change\n")
        _git("commit", "-q", "-am", "worker: say more", cwd=tree)
        worker.turn_running = False
        await h.svc.watch_tick(dash)  # the worker's turn ended
        await asyncio.wait_for(h.svc.drain(), timeout=_WAIT)

    h.run(scenario())
    (view,) = h.client.of("set_diff_view")
    assert "+worker change" in view["content"] and view["head_branch"] == rec.branch
    assert statuses[-1] == ("C1", "active")


def test_a_turn_that_does_not_end_schedules_nothing(tmp_path, repo, audits, monkeypatch):
    h, dash, _made, _statuses = _conductor_harness(tmp_path, monkeypatch)
    folder_id, _wt = h.run(
        h.svc.plan_session(
            dash,
            channel_id="C1",
            team_id="T1",
            title="t",
            channel_name="c",
            project_dir=str(repo),
            parent_folder_id="F0",
        )
    )
    dash.add_slot("chat-worker", folder_id, running=True)
    calls: list[int] = []
    monkeypatch.setattr(h.svc, "schedule_refresh", lambda _b: calls.append(1))
    for _ in range(3):
        h.run(h.svc.watch_tick(dash))
    assert calls == []
    dash._slots["chat-worker"].turn_running = False
    h.run(h.svc.watch_tick(dash))
    assert calls == [1]
    # A session filed OUTSIDE the channel's folder never counts.
    dash.add_slot("chat-elsewhere", "F0", running=True)
    h.run(h.svc.watch_tick(dash))
    dash._slots["chat-elsewhere"].turn_running = False
    h.run(h.svc.watch_tick(dash))
    assert calls == [1]


def test_two_sessions_running_in_one_worktree_are_reported(
    tmp_path, repo, audits, monkeypatch, caplog
):
    h, dash, _made, _statuses = _conductor_harness(tmp_path, monkeypatch)
    folder_id, _wt = h.run(
        h.svc.plan_session(
            dash,
            channel_id="C1",
            team_id="T1",
            title="t",
            channel_name="c",
            project_dir=str(repo),
            parent_folder_id="F0",
        )
    )
    dash.add_slot("chat-w1", folder_id, running=True)
    dash.add_slot("chat-w2", folder_id, running=True)
    with caplog.at_level("WARNING", logger=service.logger.name):
        h.run(h.svc.watch_tick(dash))
        h.run(h.svc.watch_tick(dash))
    warnings = [r for r in caplog.records if "should take turns" in r.getMessage()]
    assert len(warnings) == 1  # once per crowding, not every pass


def test_the_watch_does_nothing_with_the_gate_shut(tmp_path, repo, audits, monkeypatch):
    h, dash, _made, statuses = _conductor_harness(tmp_path, monkeypatch)
    folder_id, _wt = h.run(
        h.svc.plan_session(
            dash,
            channel_id="C1",
            team_id="T1",
            title="t",
            channel_name="c",
            project_dir=str(repo),
            parent_folder_id="F0",
        )
    )
    dash.add_slot("chat-w", folder_id, running=True)
    monkeypatch.setattr(cc_binding, "live_config", lambda orch=None: _cfg(False))
    h.run(h.svc.watch_tick(dash))
    assert statuses == []


def test_no_git_project_or_a_refused_folder_leaves_the_spec_alone(
    tmp_path, repo, audits, monkeypatch
):
    h, dash, made, _statuses = _conductor_harness(tmp_path, monkeypatch)
    plain = tmp_path / "plain"
    plain.mkdir()
    assert (
        h.run(
            h.svc.plan_session(
                dash,
                channel_id="C1",
                team_id="T1",
                title="t",
                channel_name="c",
                project_dir=str(plain),
                parent_folder_id="F0",
            )
        )
        is None
    )
    assert made == []

    async def refuse(*_a):
        return ""

    h.svc._create_folder = refuse
    assert (
        h.run(
            h.svc.plan_session(
                dash,
                channel_id="C2",
                team_id="T1",
                title="t",
                channel_name="c",
                project_dir=str(repo),
                parent_folder_id="F0",
            )
        )
        is None
    )


def test_core_plan_session_rewrites_the_spec_into_the_channel_folder(
    tmp_path, repo, audits, monkeypatch
):
    from kiro_crew.slack.code_channels.binding import SessionSpec
    from kiro_crew.slack.code_channels.workspace import core

    h, dash, _made, _statuses = _conductor_harness(tmp_path, monkeypatch)
    spec = SessionSpec(agent="kirocrew-conductor", folder_id="F0", project_dir=str(repo))
    monkeypatch.setattr(core, "_SERVICE", None)
    assert h.run(core.plan_session(dash, spec, channel_id="C1", team_id="T1", title="t")) is spec
    monkeypatch.setattr(core, "_SERVICE", h.svc)
    planned = h.run(
        core.plan_session(dash, spec, channel_id="C1", team_id="T1", title="t", channel_name="fix")
    )
    rec = h.store.get("T1", "C1")
    assert planned.agent == "kirocrew-conductor"
    assert planned.project_dir == rec.worktree and planned.folder_id == rec.folder_id != "F0"


def test_create_channel_folder_reuses_its_own_and_never_repoints_another(tmp_path, monkeypatch):
    from kiro_crew.dashboard import chat_folders
    from kiro_crew.slack.code_channels.workspace import folders as folders_mod

    dash = FakeDashboard()
    tree = tmp_path / "wt"
    tree.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    dash.add_folder("FX", "fix", "F0", str(other))  # someone else's "fix"
    created: list[dict] = []

    async def fake_create(state, *, name, parent_id, project_dir, refuse_duplicate_name):
        assert refuse_duplicate_name
        if any(f["name"] == name and f["parent_id"] == parent_id for f in state._folders):
            raise chat_folders.FolderNameExistsError()
        row = {
            "id": f"N{len(created)}",
            "name": name,
            "parent_id": parent_id,
            "project_dir": project_dir,
        }
        state._folders.append(row)
        created.append(row)
        return row

    monkeypatch.setattr(chat_folders, "create_folder_record", fake_create)
    audits: list[tuple] = []
    monkeypatch.setattr(folders_mod, "_audit", lambda *a: audits.append(a))
    first = asyncio.run(folders_mod.create_channel_folder(dash, "F0", "fix", str(tree)))
    assert first == "N0" and created[0]["name"] == "fix (wt)"  # suffixed, FX untouched
    assert dash._folders[1]["project_dir"] == str(other)
    again = asyncio.run(folders_mod.create_channel_folder(dash, "F0", "fix (wt)", str(tree)))
    assert again == "N0" and len(created) == 1  # its own folder, reused
    assert audits[0][0] == "allowed" and dash.pushes == 1


# ── outbound redaction ──────────────────────────────────────────────────────

_FAKE_KEY = "AKIA" + "IOSFODNN7EXAMPLE"


def test_a_credential_written_into_a_file_never_reaches_slack(tmp_path, repo, audits):
    h = Harness(tmp_path)
    binding, rec = _bound(h, repo)
    tree = Path(rec.worktree)
    (tree / "README.md").write_text(f"aws_access_key_id = {_FAKE_KEY}\n")
    (tree / "creds.txt").write_text(f"key={_FAKE_KEY}\n")
    h.run(h.svc.refresh(binding))
    body = h.client.of("set_diff_view")[-1]["content"]
    assert "README.md" in body and "creds.txt" in body
    assert _FAKE_KEY not in body and _FAKE_KEY not in repr(h.client.calls)


def test_context_bar_labels_go_through_redaction():
    items = build_items(BarState(branch=f"leak-{_FAKE_KEY}"))
    assert _FAKE_KEY not in repr(items)


# ── archive cleanup ─────────────────────────────────────────────────────────


def test_release_removes_a_clean_worktree_and_its_unused_branch(tmp_path, repo, audits):
    h = Harness(tmp_path)
    _binding, rec = _bound(h, repo)
    assert h.run(h.svc.release("T1", "C1")) == "removed"
    assert not Path(rec.worktree).exists()
    assert _git("branch", "--list", rec.branch, cwd=repo) == ""
    assert h.store.get("T1", "C1").status == "released"


def test_release_keeps_the_branch_when_it_has_commits(tmp_path, repo, audits):
    h = Harness(tmp_path)
    _binding, rec = _bound(h, repo)
    (Path(rec.worktree) / "README.md").write_text("done\n")
    _git("commit", "-q", "-am", "work", cwd=Path(rec.worktree))
    assert h.run(h.svc.release("T1", "C1")) == "removed"
    assert rec.branch in _git("branch", "--list", rec.branch, cwd=repo)


@pytest.mark.parametrize("dirty", ["modified", "untracked"])
def test_release_never_deletes_uncommitted_work(tmp_path, repo, audits, dirty):
    h = Harness(tmp_path)
    _binding, rec = _bound(h, repo)
    tree = Path(rec.worktree)
    if dirty == "modified":
        (tree / "README.md").write_text("unsaved idea\n")
    else:
        (tree / "notes.md").write_text("unsaved idea\n")
    assert h.run(h.svc.release("T1", "C1")) == "kept"
    assert tree.is_dir() and "unsaved idea" in "".join(p.read_text() for p in tree.glob("*.md"))
    assert h.store.get("T1", "C1").status == "kept"
    assert ("code_channel_worktree_release", "denied") in [(a[0], a[1]) for a in audits]


def test_release_keeps_a_tree_whose_only_content_is_ignored_files(tmp_path, repo, audits):
    h = Harness(tmp_path)
    _binding, rec = _bound(h, repo)
    tree = Path(rec.worktree)
    (tree / ".gitignore").write_text("*.env\n")
    _git("add", ".gitignore", cwd=tree)
    _git("commit", "-q", "-m", "ignore env", cwd=tree)
    (tree / "local.env").write_text("TOKEN=mine\n")
    assert h.run(h.svc.release("T1", "C1")) == "kept"
    assert (tree / "local.env").read_text() == "TOKEN=mine\n"


def test_release_keeps_commits_made_on_a_detached_head(tmp_path, repo, audits):
    h = Harness(tmp_path)
    _binding, rec = _bound(h, repo)
    tree = Path(rec.worktree)
    _git("checkout", "-q", "--detach", cwd=tree)
    (tree / "README.md").write_text("detached work\n")
    _git("commit", "-q", "-am", "detached", cwd=tree)
    head = _git("rev-parse", "HEAD", cwd=tree)
    assert h.run(h.svc.release("T1", "C1")) == "kept"
    assert _git("rev-parse", "HEAD", cwd=tree) == head


def test_after_archive_no_hook_recreates_the_worktree(tmp_path, repo, audits):
    h = Harness(tmp_path)
    binding, rec = _bound(h, repo)
    assert h.run(h.svc.release("T1", "C1")) == "removed"
    calls = list(h.client.calls)
    assert h.run(h.svc.ensure(binding)) is None
    h.run(h.svc.refresh(binding))
    assert not Path(rec.worktree).exists() and h.client.calls == calls


def test_an_archive_before_any_record_still_stops_a_later_bind(tmp_path, repo, audits):
    """Archived before a bind stored anything: the tombstone keeps the channel off."""
    h = Harness(tmp_path)
    assert h.run(h.svc.release("T1", "C1")) == "no_record"
    assert h.run(h.svc.ensure(FakeBinding("C1", project_dir=str(repo)))) is None
    assert _git("worktree", "list", "--porcelain", cwd=repo).count("worktree ") == 1


def test_a_bind_finishing_after_the_archive_discards_its_fresh_worktree(
    tmp_path, repo, audits, monkeypatch
):
    h = Harness(tmp_path)
    binding = FakeBinding("C1", project_dir=str(repo))
    real_bind = h.svc._bind

    async def bind_then_archive(b):
        made = await real_bind(b)
        # The archive lands while this bind is between cutting and storing.
        await asyncio.to_thread(service._release_sync, h.store, "T1", "C1")
        return made

    monkeypatch.setattr(h.svc, "_bind", bind_then_archive)
    assert h.run(h.svc.ensure(binding)) is None
    assert _git("worktree", "list", "--porcelain", cwd=repo).count("worktree ") == 1
    assert _git("branch", "--list", "slack/*", cwd=repo) == ""


def test_module_level_release_serializes_with_the_registered_service(tmp_path, repo, audits):
    class Registry:
        def on_turn_start(self, hook):
            pass

        def on_turn_end(self, hook):
            pass

    h = Harness(tmp_path)
    _binding, rec = _bound(h, repo)
    service.register(Registry(), h.svc)
    try:
        assert h.run(service.release_channel_workspace("T1", "C1")) == "removed"
    finally:
        service._SERVICE = None
    assert h.store.get("T1", "C1").status == "released"


def test_a_binding_reporting_the_worktree_as_its_project_is_the_same_workspace(
    tmp_path, repo, audits
):
    h = Harness(tmp_path)
    _binding, rec = _bound(h, repo)
    again = h.run(h.svc.ensure(FakeBinding("C1", project_dir=rec.worktree)))
    assert again.worktree == rec.worktree and again.repo_root == rec.repo_root


# ── pure helpers ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://github.com/acme/widgets.git", RepoRef("github.com", "acme", "widgets")),
        ("https://user:tok@GitHub.com/acme/widgets", RepoRef("github.com", "acme", "widgets")),
        ("git@github.com:acme/widgets.git", RepoRef("github.com", "acme", "widgets")),
        ("ssh://git@github.com/acme/widgets.git", RepoRef("github.com", "acme", "widgets")),
        ("https://gitlab.com/group/sub/proj.git", None),
        ("file:///tmp/x.git", None),
        ("/local/path/repo", None),
        ("https://github.com/acme/..", None),
        ("https://github.com:8443/acme/widgets", None),
        ("", None),
    ],
)
def test_parse_remote(url, expected):
    assert parse_remote(url) == expected


def test_branch_names_are_channel_unique_and_always_valid():
    a = git_ops.channel_branch("Fix the widget!", "T1", "C1")
    b = git_ops.channel_branch("Fix the widget!", "T1", "C2")
    assert a != b and a.startswith("slack/fix-the-widget-")
    assert git_ops.channel_branch("", "T1", "C1").startswith("slack/code-channel-")
    assert git_ops.channel_branch("💥 ..lock ../", "T1", "C1").startswith("slack/")
    assert len(git_ops.channel_branch("x" * 500, "T1", "C1")) <= 80


def test_context_bar_never_exceeds_five_unique_items_with_valid_icons():
    pr = PullRequest(1, "https://github.com/a/b/pull/1", "open", "failed")
    items = build_items(
        BarState(repo=RepoRef("github.com", "a", "b"), branch="br", branch_pushed=True, pr=pr)
    )
    assert [i["key"] for i in items] == ["repo", "branch", "pr", "ci"]
    assert items[1]["url"] == "https://github.com/a/b/tree/br"
    assert all(i["icon"] in ICONS and "item_type" not in i for i in items)
    long = build_items(BarState(folder="/x/" + "y" * 300))
    assert len(long[0]["label"]) <= 128


def test_assemble_cuts_at_a_file_boundary():
    one = b"diff --git a/a b/a\n" + b"+" + b"a" * 300 + b"\n"
    two = b"diff --git a/b b/b\n" + b"+" + b"b" * 300 + b"\n"
    built = assemble(
        one + two, tracked_truncated=False, untracked=[], untracked_truncated=False, limit=900
    )
    assert built.truncated and built.files == 1
    assert "a/b b/b" not in built.content and built.content.startswith("Diff truncated")
    assert len(built.content.encode()) <= 900


def test_render_untracked_refuses_unsafe_paths(tmp_path):
    (tmp_path / "ok.txt").write_text("a\nb\n")
    assert render_untracked(str(tmp_path), "ok.txt", budget=10_000).endswith(
        b"@@ -0,0 +1,2 @@\n+a\n+b\n"
    )
    assert render_untracked(str(tmp_path), "../ok.txt", budget=10_000) == OMITTED
    assert render_untracked(str(tmp_path), "bad\nname", budget=10_000) == OMITTED
    assert render_untracked(str(tmp_path), "ok.txt", budget=10) == OMITTED


@pytest.mark.skipif(not diff_mod._DIR_FD_OK, reason="no dir_fd support on this platform")
def test_render_untracked_never_reads_through_a_symlinked_worktree_root(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "id_rsa").write_text("PRIVATE\n")
    os.symlink(outside, tmp_path / "tree")
    assert render_untracked(str(tmp_path / "tree"), "id_rsa", budget=10_000) is None


@pytest.mark.skipif(not diff_mod._DIR_FD_OK, reason="no dir_fd support on this platform")
def test_render_untracked_never_reads_a_hard_linked_file(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP-SECRET\n")
    root = tmp_path / "tree"
    root.mkdir()
    os.link(secret, root / "notes.txt")
    assert render_untracked(str(root), "notes.txt", budget=10_000) == OMITTED


@pytest.mark.skipif(not diff_mod._DIR_FD_OK, reason="no dir_fd support on this platform")
def test_render_untracked_closes_every_descriptor_it_opens(tmp_path):
    """Links, FIFOs and oversized files return early: none may leak a dir fd."""
    root = tmp_path / "tree"
    (root / "d").mkdir(parents=True)
    os.symlink("/etc/hostname", root / "d" / "link")
    (root / "d" / "big.txt").write_text("x" * 100)
    if hasattr(os, "mkfifo"):
        os.mkfifo(root / "d" / "pipe")
    fd_dir = "/proc/self/fd" if os.path.isdir("/proc/self/fd") else "/dev/fd"
    before = len(os.listdir(fd_dir))
    for _ in range(50):
        for rel in ("d/link", "d/big.txt", "d/pipe", "d/missing"):
            render_untracked(str(root), rel, budget=10)
    assert len(os.listdir(fd_dir)) == before


@pytest.mark.skipif(not diff_mod._DIR_FD_OK, reason="no dir_fd support on this platform")
def test_render_untracked_never_reads_through_a_symlinked_parent(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / ".env").write_text("SLACK_BOT_TOKEN=xoxb-REAL\n")
    root = tmp_path / "tree"
    root.mkdir()
    os.symlink(outside, root / "d")
    assert render_untracked(str(root), "d/.env", budget=10_000) is None


@pytest.mark.skipif(not hasattr(os, "mkfifo") or not diff_mod._DIR_FD_OK, reason="POSIX only")
def test_render_untracked_refuses_a_fifo_swapped_in_after_the_lstat(tmp_path, monkeypatch):
    """The open is non-blocking and the fd must be the file the lstat saw."""
    (tmp_path / "decoy.txt").write_text("x\n")
    os.mkfifo(tmp_path / "pipe")
    real_stat = os.stat

    def swapped(path, *a, **kw):
        if path == "pipe":  # the leaf lstat sees a regular file...
            return real_stat(tmp_path / "decoy.txt")
        return real_stat(path, *a, **kw)

    monkeypatch.setattr(diff_mod.os, "stat", swapped)
    # ...but the open lands on the FIFO: must neither block nor render.
    assert render_untracked(str(tmp_path), "pipe", budget=10_000) is None


def test_invalid_utf8_near_the_limit_is_cut_with_the_note():
    line = b"+" + b"\xe9" * 99 + b"\n"
    body = b"diff --git a/x b/x\n" + line * 5000  # ~500 KB raw, ~1.5 MB once decoded
    built = assemble(body, tracked_truncated=False, untracked=[], untracked_truncated=False)
    assert built.truncated and built.content.startswith("Diff truncated")
    assert len(built.content.encode("utf-8")) <= MAX_VIEW_BYTES


def test_a_new_file_too_large_to_show_marks_the_diff_truncated(tmp_path, repo, audits):
    h = Harness(tmp_path)
    binding, rec = _bound(h, repo)
    tree = Path(rec.worktree)
    (tree / "README.md").write_text("small change\n")
    (tree / "big.txt").write_text("y" * 1_200_000)
    h.run(h.svc.refresh(binding))
    body = h.client.of("set_diff_view")[-1]["content"]
    assert body.startswith("Diff truncated") and "big.txt" not in body
    assert "+small change" in body


def test_an_archived_channel_with_kept_work_drops_out_of_the_watch(
    tmp_path, repo, audits, monkeypatch
):
    h, dash, _made, statuses = _conductor_harness(tmp_path, monkeypatch)
    folder_id, worktree = h.run(
        h.svc.plan_session(
            dash,
            channel_id="C1",
            team_id="T1",
            title="t",
            channel_name="c",
            project_dir=str(repo),
            parent_folder_id="F0",
        )
    )
    (Path(worktree) / "notes.md").write_text("unsaved idea\n")
    assert h.run(h.svc.release("T1", "C1")) == "kept"
    calls: list[int] = []
    monkeypatch.setattr(h.svc, "schedule_refresh", lambda _b: calls.append(1))
    worker = dash.add_slot("chat-w", folder_id, running=True)
    h.run(h.svc.watch_tick(dash))
    worker.turn_running = False
    h.run(h.svc.watch_tick(dash))
    assert calls == [] and statuses == []
