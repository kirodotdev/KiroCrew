"""The work board's per-worker PR and CI, the Code tab's combined diff, the wrap-up's outcomes.

Shares the board suites' fixtures and helpers (imported, so they apply here too).
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_slack_code_channel_board as suite
import test_slack_code_channel_board_live as live
import test_slack_code_channel_board_review_rounds as rounds
from push_guard_helpers import _fixture_git_env

from kiro_crew.slack.code_channels import board as board_pkg
from kiro_crew.slack.code_channels import surface, wrapup
from kiro_crew.slack.code_channels.board import links, model, publisher, render, source
from kiro_crew.slack.code_channels.client import SCOPE_REFUSED
from kiro_crew.slack.code_channels.models import CodeChannelApiError
from kiro_crew.slack.code_channels.workspace import git_ops, service, worker_diff
from kiro_crew.slack.code_channels.workspace.diff import (
    CLEAN,
    DiffGroup,
    assemble_groups,
    screen_tracked,
)
from kiro_crew.slack.code_channels.workspace.pr import PrLookup, PullRequest

# The board suites' fixtures (autouse ones included), bound here so they apply.
_no_sandbox = suite._no_sandbox
_feature_on = suite._feature_on
_surface_store = suite._surface_store
_pinned_identity = suite._pinned_identity
publisher_clock = suite.publisher_clock
slack = suite.slack
lineage = live.lineage
_Harness = suite._Harness
_FakeBinding = suite._FakeBinding
_binding = suite._binding
_link = suite._link
_row = suite._row
_git = suite._git
_Slots = live._Slots
_live_slot = live._live_slot

_URL = "https://github.com/acme/widgets/pull/"


@pytest.fixture(scope="module")
def _repo_template(tmp_path_factory) -> Path:
    """``suite.repo``'s bare remote and clone, built once per module.

    It runs before the per-test git floor applies, so its git calls use the scrubbed
    fixture env: no host config, templates, hooks or maintenance reach it.
    """
    base = tmp_path_factory.mktemp("board-diff-repo")
    env = _fixture_git_env()

    def git(*args: str, cwd: Path) -> None:
        subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True)

    remote = base / "remote.git"
    git("init", "-q", "--bare", "-b", "main", str(remote), cwd=base)
    root = base / "proj"
    root.mkdir()
    git("init", "-q", "-b", "main", cwd=root)
    for key, value in (("user.email", "t@example.com"), ("user.name", "T"), ("gc.auto", "0")):
        git("config", key, value, cwd=root)
    (root / "README.md").write_text("hello\n")
    git("add", ".", cwd=root)
    git("commit", "-q", "-m", "init", cwd=root)
    git("remote", "add", "origin", "https://github.com/acme/widgets.git", cwd=root)
    git("push", "-q", str(remote), "main:main", cwd=root)
    git("fetch", "-q", str(remote), "+refs/heads/*:refs/remotes/origin/*", cwd=root)
    git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main", cwd=root)
    return base


@pytest.fixture
def repo(tmp_path, _repo_template) -> Path:
    """A private copy of :func:`_repo_template`, laid out as ``suite.repo`` lays it out."""
    for name in ("remote.git", "proj"):
        shutil.copytree(_repo_template / name, tmp_path / name, symlinks=True)
    return tmp_path / "proj"


def _groups(cwd: str, board, channel_branch: str, trees: dict) -> list[DiffGroup]:
    """The Code tab's worker groups as the service builds them."""
    return worker_diff.worker_groups(
        cwd,
        board,
        channel_branch,
        trees,
        scan_bytes=service._SCAN_BYTES,
        screen=lambda raw, cut: service._screen_worker(cwd, raw, cut),
        header=service._worker_header,
    )


def _worker_slots(monkeypatch, trees: dict) -> None:
    """Live worker slots with their projects, where ``BoardCache._worker_projects`` reads them."""
    slots = {key: SimpleNamespace(project=str(path)) for key, path in trees.items()}
    monkeypatch.setattr(surface, "dashboard_state", lambda: SimpleNamespace(_slots=slots))


def _worker_branch(repo: Path, tree: Path, branch: str, name: str, text: str) -> None:
    _git("worktree", "add", "-q", "-b", branch, str(tree), "main", cwd=repo)
    (tree / name).write_text(text)
    _git("add", name, cwd=tree)
    _git("commit", "-q", "-m", branch, cwd=tree)


_WORKER_PEM_BODY = ["MIIEowIBAAKCAQEA" + "A" * 48] + [f"{n:02d}" + "Qk" * 31 for n in range(20)]


def _worker_pem(lines: list[str]) -> str:
    """An RSA key block whose armour is assembled at run time, so no literal sits here."""
    label = "RSA " + "PRI" + "VATE " + "KEY"
    dash = "-" * 5
    return f"{dash}BEGIN {label}{dash}\n" + "\n".join(lines) + f"\n{dash}END {label}{dash}\n"


def _two_worker_branches(tmp_path, repo):
    (repo / "big.txt").write_text("".join(f"line {n}\n" for n in range(400)))
    _git("add", ".", cwd=repo)
    _git("commit", "-q", "-m", "big", cwd=repo)
    for name in ("feat/w1", "feat/w2"):
        tree = tmp_path / name.replace("/", "-")
        _git("worktree", "add", "-q", "-b", name, str(tree), "main", cwd=repo)
        (tree / "big.txt").write_text((tree / "big.txt").read_text().replace("line 7\n", "X\n"))
        _git("commit", "-q", "-am", name, cwd=tree)
    return model.BoardSnapshot(
        rows=(
            _row("it_00000001", model.WORKING, title="One", branch="feat/w1", session_key="w1"),
            _row("it_00000002", model.WORKING, title="Two", branch="feat/w2", session_key="w2"),
        )
    )


def _two_trees(tmp_path) -> dict[str, str]:
    """``_two_worker_branches``' workers, each in its own tree."""
    return {"w1": str(tmp_path / "feat-w1"), "w2": str(tmp_path / "feat-w2")}


# ── the combined diff ───────────────────────────────────────────────────────


def test_combined_diff_groups_each_workers_commits_under_a_header(tmp_path, repo, monkeypatch):
    worker_tree = tmp_path / "worker-wt"
    _worker_branch(repo, worker_tree, "feat/login", "login.py", "def login():\n    return True\n")
    _git("branch", "feat/merged", "main", cwd=repo)  # no commits of its own
    _worker_slots(monkeypatch, {"chat-w1": worker_tree})
    snap = model.BoardSnapshot(
        goal="g",
        rows=(
            _row(
                "it_00000001",
                model.WORKING,
                title="Fix login",
                branch="feat/login",
                pr_number=12,
                session_key="chat-w1",
            ),
            _row("it_00000002", model.DONE, title="Nothing new", branch="feat/merged"),
            _row("it_00000003", model.IDLE, title="Bad branch", branch="no/such/branch"),
        ),
    )
    pr = PrLookup("found", PullRequest(12, _URL + "12", "open", "passed"))
    # The worker reported #12, so #12 is what is read (never a PR found by branch).
    h = _Harness(tmp_path, snapshot=snap, prs={"#12": pr})
    binding = _FakeBinding(project_dir=str(repo))
    rec = asyncio.run(h.svc.ensure(binding))
    (Path(rec.worktree) / "README.md").write_text("hello\nchannel edit\n")
    asyncio.run(h.svc.refresh(binding))
    content = h.client.last("set_diff_view")["content"]
    channel_at = content.index(f"==== Channel branch {rec.branch} (conductor) ====")
    worker_at = content.index(
        "==== Helper agent 1 of 3: Fix login · branch feat/login · PR #12 open · tests passing"
        " · working ===="
    )
    assert channel_at < content.index("+channel edit") < worker_at < content.index("+def login():")
    assert "Nothing new" not in content and "Bad branch" not in content
    # The published board carries the forge's PR reading.
    first = h.published[-1].rows[0]
    assert first.pr_url.endswith("/pull/12") and first.pr_ci == "passed"
    # The bar stays the channel's own: no helper agent rides it.
    labels = [i["label"] for i in h.client.last("set_context_bar")["items"]]
    assert not any("Fix login" in label for label in labels)


def test_no_board_keeps_the_plain_single_diff(tmp_path, repo):
    h = _Harness(tmp_path)
    binding = _FakeBinding(project_dir=str(repo))
    rec = asyncio.run(h.svc.ensure(binding))
    (Path(rec.worktree) / "README.md").write_text("hello\nchannel edit\n")
    asyncio.run(h.svc.refresh(binding))
    content = h.client.last("set_diff_view")["content"]
    assert "+channel edit" in content and "====" not in content


def test_a_worker_commit_inside_a_private_key_never_reaches_the_combined_diff(
    tmp_path, repo, monkeypatch
):
    """A worker hunk 10 lines into a PEM carries no marker; the whole-file screen still sees it."""
    (repo / "fixtures").mkdir()
    (repo / "fixtures" / "test_key.txt").write_text(_worker_pem(_WORKER_PEM_BODY))
    _git("add", ".", cwd=repo)
    _git("commit", "-q", "-m", "fixture key", cwd=repo)
    worker_tree = tmp_path / "worker-wt"
    _git("worktree", "add", "-q", "-b", "feat/keys", str(worker_tree), "main", cwd=repo)
    edited = list(_WORKER_PEM_BODY)
    edited[10] = "ZZ" + edited[10][2:]
    (worker_tree / "fixtures" / "test_key.txt").write_text(_worker_pem(edited))
    (worker_tree / "visible.py").write_text("VISIBLE = 1\n")
    _git("add", ".", cwd=worker_tree)
    _git("commit", "-q", "-m", "rotate", cwd=worker_tree)
    _worker_slots(monkeypatch, {"chat-w1": worker_tree})
    snap = model.BoardSnapshot(
        rows=(
            _row(
                "it_00000001",
                model.WORKING,
                title="Rotate",
                branch="feat/keys",
                session_key="chat-w1",
            ),
        )
    )
    h = _Harness(tmp_path, snapshot=snap)
    binding = _FakeBinding(project_dir=str(repo))
    rec = asyncio.run(h.svc.ensure(binding))
    (Path(rec.worktree) / "README.md").write_text("hello\nchannel edit\n")
    asyncio.run(h.svc.refresh(binding))
    content = h.client.last("set_diff_view")["content"]
    assert "+VISIBLE = 1" in content and content.startswith("Diff truncated")
    assert "test_key.txt" not in content
    for line in edited + _WORKER_PEM_BODY:
        assert line not in content and line not in repr(h.client.calls)


def test_a_worker_range_read_hands_the_screen_whole_files_whatever_the_repo_config(tmp_path, repo):
    """Like tracked_diff: whole-file context, and repo diff config cannot reshape the read."""
    lines = [f"line {n}" for n in range(40)]
    (repo / "a.txt").write_text("\n".join(lines) + "\n", newline="\n")
    _git("add", ".", cwd=repo)
    _git("commit", "-q", "-m", "a", cwd=repo)
    base = _git("rev-parse", "HEAD", cwd=repo)
    lines[20] = "CHANGED"
    (repo / "a.txt").write_text("\n".join(lines) + "\n", newline="\n")
    _git("commit", "-q", "-am", "b", cwd=repo)
    head = _git("rev-parse", "HEAD", cwd=repo)
    for key in ("diff.noprefix", "diff.mnemonicPrefix", "diff.suppressBlankEmpty"):
        _git("config", key, "true", cwd=repo)
    full, cut = git_ops.commit_range_diff(str(repo), base, head, cap=10_000_000)
    seen: list[str] = []
    asked: list[str] = []
    screened, omitted = screen_tracked(
        full,
        truncated=cut,
        classify=lambda s: seen.append(s) or CLEAN,
        mask=str,
        sensitive=lambda p: asked.append(p) or False,
    )
    assert not omitted and b"+CHANGED" in screened
    assert set(asked) == {"a.txt"}
    assert "\n".join(lines) in seen  # the screen classified the whole new version


def test_worker_reads_share_one_raw_scan_budget_and_say_what_they_left_out(
    tmp_path, repo, monkeypatch
):
    """Whole-file reads screen down to a few lines, so the RAW bytes bound the work."""
    board = _two_worker_branches(tmp_path, repo)
    reads: list[int] = []
    real = git_ops.commit_range_diff

    def counted(cwd, base, head, *, cap):
        reads.append(cap)
        return real(cwd, base, head, cap=cap)

    monkeypatch.setattr(git_ops, "commit_range_diff", counted)
    monkeypatch.setattr(service, "_SCAN_BYTES", 5000)  # big.txt's whole-file read is ~3.4 KB
    _groups(str(repo), board, "main", _two_trees(tmp_path))
    assert len(reads) == 2 and reads[1] < reads[0] == 5000  # the second read gets what is left
    monkeypatch.setattr(service, "_SCAN_BYTES", 3000)  # one read spends it all
    reads.clear()
    groups = _groups(str(repo), board, "main", _two_trees(tmp_path))
    assert len(reads) == 1  # the second branch is never read
    assert groups[-1] == DiffGroup("", b"", True)  # the left-out marker, not the cut read
    built = assemble_groups(groups)
    assert built.truncated and built.content.startswith("Diff truncated")


def test_workers_with_nothing_of_their_own_leave_no_truncation_note(tmp_path, repo):
    _git("branch", "feat/merged", "main", cwd=repo)
    board = model.BoardSnapshot(
        rows=(
            _row("it_00000001", model.DONE, title="Merged", branch="feat/merged"),
            _row("it_00000002", model.IDLE, title="Bad", branch="no/such/branch"),
        )
    )
    assert _groups(str(repo), board, "main", {}) == []


def test_a_worker_whose_range_read_fails_is_reported_as_left_out(tmp_path, repo, monkeypatch):
    board = _two_worker_branches(tmp_path, repo)
    monkeypatch.setattr(git_ops, "commit_range_diff", lambda *a, **k: None)
    built = assemble_groups(_groups(str(repo), board, "main", _two_trees(tmp_path)))
    assert built.truncated and built.content.startswith("Diff truncated")


def test_a_tree_listing_git_cannot_give_is_reported_as_left_out(tmp_path, repo, monkeypatch):
    """Opus on bcf6443265: an older git's failed listing dropped every worker silently."""
    board = _two_worker_branches(tmp_path, repo)
    monkeypatch.setattr(git_ops, "worktree_heads", lambda cwd: {})
    assert _groups(str(repo), board, "main", _two_trees(tmp_path)) == [DiffGroup("", b"", True)]


def test_assemble_groups_shares_one_budget_and_says_when_it_cut():
    big = b"diff --git a/x b/x\n" + b"+" + b"y" * 3000 + b"\n"
    groups = [DiffGroup("Channel", big), DiffGroup("Worker 1", big), DiffGroup("Empty", b"")]
    built = assemble_groups(groups, limit=4500)
    assert built.truncated and built.content.startswith("Diff truncated")
    assert built.content.index("==== Channel ====") < built.content.index("==== Worker 1 ====")
    assert built.content.count("y") < 2 * 3000  # the second group was cut short
    whole = assemble_groups(groups)
    assert not whole.truncated and whole.files == 2 and "Empty" not in whole.content


def test_branch_commit_never_resolves_option_or_namespace_shaped_names(tmp_path, repo):
    assert git_ops.branch_commit(str(repo), "main")
    for bad in ("--all", "HEAD~1", "refs/tags/x", "a..b", ""):
        assert git_ops.branch_commit(str(repo), bad) == ""
    assert git_ops.commit_range_diff(str(repo), "HEAD", "main", cap=100) is None


def test_the_combined_diff_channel_header_passes_the_outbound_redaction(
    tmp_path, repo, monkeypatch
):
    _worker_branch(repo, tmp_path / "wt2", "feat/x", "x.py", "x = 1\n")
    real = service.redact_via_context
    monkeypatch.setattr(
        service, "redact_via_context", lambda text: real(text).replace("Channel branch", "[R]")
    )
    _worker_slots(monkeypatch, {"chat-w1": tmp_path / "wt2"})
    snap = model.BoardSnapshot(
        rows=(_row("it_00000001", model.WORKING, branch="feat/x", session_key="chat-w1"),)
    )
    h = _Harness(tmp_path, snapshot=snap)
    binding = _FakeBinding(project_dir=str(repo))
    rec = asyncio.run(h.svc.ensure(binding))
    (Path(rec.worktree) / "README.md").write_text("edit\n")
    asyncio.run(h.svc.refresh(binding))
    assert f"==== [R] {rec.branch} (conductor) ====" in h.client.last("set_diff_view")["content"]


def test_a_worker_header_and_commits_pass_the_outbound_redaction(tmp_path, repo, monkeypatch):
    """The worker group's header and body go through the service's redactor, not around it."""
    _worker_branch(repo, tmp_path / "wt3", "feat/y", "y.py", "TOKEN = 1\n")
    real = service.redact_via_context
    monkeypatch.setattr(
        service, "redact_via_context", lambda text: real(text).replace("Helper agent", "[H]")
    )
    monkeypatch.setattr(service, "_redact_bytes", lambda data: data.replace(b"TOKEN", b"[T]"))
    board = model.BoardSnapshot(
        rows=(_row("it_00000001", model.WORKING, title="Y", branch="feat/y", session_key="w"),)
    )
    [group] = _groups(str(repo), board, "main", {"w": str(tmp_path / "wt3")})
    assert group.header.startswith("[H] 1 of 1: Y · branch feat/y")
    assert b"+[T] = 1" in group.tracked and b"TOKEN" not in group.tracked


def test_the_combined_diff_covers_every_worker_the_plan_shows(tmp_path, repo):
    """21 workers: the plan shows them all, so the Code tab reads all 21."""
    rows = []
    trees = {}
    for n in range(1, 22):
        name = f"feat/w{n}"
        tree = tmp_path / f"wt{n}"
        _worker_branch(repo, tree, name, f"w{n}.txt", f"worker {n}\n")
        rows.append(
            _row(f"it_{n:08x}", model.WORKING, title=f"W{n}", branch=name, session_key=name)
        )
        trees[name] = str(tree)
    board = model.BoardSnapshot(rows=tuple(rows))
    assert len(board.plan_rows) == 21 and len(board.shown) == 20
    built = assemble_groups(_groups(str(repo), board, "main", trees))
    assert "w21.txt" in built.content and not built.truncated


def test_a_worker_the_plan_leaves_out_is_reported_as_left_out(tmp_path, repo, monkeypatch):
    monkeypatch.setattr(model, "MAX_TASKS", 2)
    rows = []
    trees = {}
    for n in range(1, 4):
        name = f"feat/x{n}"
        tree = tmp_path / f"x{n}"
        _worker_branch(repo, tree, name, f"x{n}.txt", f"x {n}\n")
        rows.append(
            _row(f"it_{n:08x}", model.WORKING, title=f"X{n}", branch=name, session_key=name)
        )
        trees[name] = str(tree)
    built = assemble_groups(
        _groups(str(repo), model.BoardSnapshot(rows=tuple(rows)), "main", trees)
    )
    assert built.truncated and "x1.txt" in built.content


def test_settled_workers_beyond_the_view_are_not_read_for_the_diff(tmp_path, repo, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(git_ops, "branch_commit", lambda cwd, branch: calls.append(branch))
    open_rows = [_row(f"it_{n:08x}", model.WORKING, branch=f"feat/o{n}") for n in range(1, 21)]
    done = [_row(f"it_{n:08x}", model.DONE, branch=f"feat/d{n}") for n in range(21, 24)]
    _groups(str(repo), model.BoardSnapshot(rows=tuple(open_rows + done)), "main", {})
    assert not any(b.startswith("feat/d") for b in calls) and len(calls) == 20


def test_a_worker_naming_another_channels_branch_shows_no_commits(tmp_path, repo):
    """Adversarial: a worker-reported branch decided which commits the Code tab read."""
    for name, tree, path in (
        ("feat/mine", tmp_path / "wt-mine", "mine.txt"),
        ("feat/other-channel", tmp_path / "wt-other", "other.txt"),
    ):
        _git("worktree", "add", "-q", "-b", name, str(tree), "main", cwd=repo)
        (tree / "src").mkdir()
        (tree / path).write_text(f"{name}\n")
        _git("add", path, cwd=tree)
        _git("commit", "-q", "-m", name, cwd=tree)
    rows = (
        _row("it_00000001", title="Mine", branch="feat/mine", session_key="w1"),
        _row("it_00000002", title="Claims", branch="feat/other-channel", session_key="w2"),
        _row("it_00000003", title="Closed", branch="feat/mine", session_key="gone"),
    )
    trees = {"w1": str(tmp_path / "wt-mine" / "src"), "w2": str(tmp_path / "wt-mine")}
    groups = _groups(str(repo), model.BoardSnapshot(rows=rows), "main", trees)
    built = assemble_groups(groups)
    assert "mine.txt" in built.content and "other.txt" not in built.content
    headers = [g.header for g in groups]
    assert len(headers) == 1 and ": Mine · branch feat/mine" in headers[0]


# ── the Code tab's scope ────────────────────────────────────────────────────


def _secret_worker(tmp_path, repo, monkeypatch):
    _worker_branch(repo, tmp_path / "wt-secret", "feat/secret", "secret_plan.txt", "worker\n")
    _worker_slots(monkeypatch, {"chat-w1": tmp_path / "wt-secret"})
    return model.BoardSnapshot(
        rows=(_row(title="Secret work", branch="feat/secret", session_key="chat-w1"),)
    )


def test_a_scope_refused_during_the_diff_build_sends_no_worker_content(tmp_path, repo, monkeypatch):
    """The diff's git work takes seconds: the scope is judged again before it is sent."""
    h = _Harness(tmp_path, snapshot=_secret_worker(tmp_path, repo, monkeypatch))
    binding = _FakeBinding(project_dir=str(repo))
    rec = asyncio.run(h.svc.ensure(binding))
    (Path(rec.worktree) / "README.md").write_text("channel change\n")
    real_build = h.svc._build_diff
    admitted = {"now": True}

    def build_then_share(record, board_arg=None, trees=None):
        out = real_build(record, board_arg, trees)
        if board_arg is not None:
            assert "secret_plan.txt" in out[0]  # the worker's change was in the build
            admitted["now"] = False  # the channel turned shared during the git work
        return out

    monkeypatch.setattr(h.svc, "_build_diff", build_then_share)
    source.set_scope_predicate(lambda state, b: admitted["now"])
    asyncio.run(h.svc.refresh(binding))
    content = h.client.last("set_diff_view")["content"]
    assert "README.md" in content
    assert "secret_plan.txt" not in content and "Secret work" not in content
    assert rec.key not in h.svc._boards and h.published == []  # nor the board


def test_a_tab_refused_at_its_send_goes_again_as_the_channels_own(tmp_path, repo, monkeypatch):
    """The client asks the board's scope last: a share recorded at the send sends no worker."""
    h = _Harness(tmp_path, snapshot=_secret_worker(tmp_path, repo, monkeypatch))
    binding = _FakeBinding(project_dir=str(repo))
    rec = asyncio.run(h.svc.ensure(binding))
    (Path(rec.worktree) / "README.md").write_text("channel change\n")
    admitted = {"now": True}
    real_call = h.svc._call

    async def call(method, record, *args, **kwargs):
        if method == "set_diff_view" and kwargs.get("gate") is not None:
            admitted["now"] = False  # recorded during the client's own awaits
        return await real_call(method, record, *args, **kwargs)

    monkeypatch.setattr(h.svc, "_call", call)
    source.set_scope_predicate(lambda state, b: admitted["now"])
    asyncio.run(h.svc.refresh(binding))
    [content] = [kw["content"] for m, kw in h.client.calls if m == "set_diff_view"]
    assert "README.md" in content and "secret_plan.txt" not in content
    assert h.published == []


def test_a_tab_fenced_by_an_off_at_its_send_sends_no_channel_only_tab(tmp_path, repo, monkeypatch):
    """Set to Off or Review during the refresh: the combined tab is fenced at the
    send, and the channel-only fallback asks the same fence (no tab goes out)."""
    h = _Harness(tmp_path, snapshot=_secret_worker(tmp_path, repo, monkeypatch))
    binding = _FakeBinding(project_dir=str(repo))
    rec = asyncio.run(h.svc.ensure(binding))
    (Path(rec.worktree) / "README.md").write_text("channel change\n")
    on = {"now": True}
    monkeypatch.setattr(service, "_activation_admits", lambda b: on["now"])
    real_call = h.svc._call

    async def call(method, record, *args, **kwargs):
        if method == "set_diff_view" and kwargs.get("gate") is not None:
            on["now"] = False  # the operator sets the channel Off during the refresh
        return await real_call(method, record, *args, **kwargs)

    monkeypatch.setattr(h.svc, "_call", call)
    source.set_scope_predicate(lambda state, b: on["now"])
    board = model.BoardSnapshot(rows=(_row(title="Fix login"),))
    assert not asyncio.run(h.svc._publish_diff(rec, binding, board))
    assert [m for m, _kw in h.client.calls if m == "set_diff_view"] == []


def test_a_failed_channel_only_rebuild_takes_the_worker_tab_down(tmp_path, repo, monkeypatch):
    h = _Harness(tmp_path)
    binding = _FakeBinding(project_dir=str(repo))
    asyncio.run(h.svc.ensure(binding))
    rec = h.svc.store.get("T1", "C1")
    rec.diff_hash, rec.view_id = "old", "V1"  # a combined tab is up
    calls = {"n": 0}

    def build(record, board_arg=None, trees=None):
        calls["n"] += 1
        return ("diff --git a/x b/x\n", "main", "w") if board_arg is not None else None

    monkeypatch.setattr(h.svc, "_build_diff", build)
    source.set_scope_predicate(lambda state, b: False)
    board = model.BoardSnapshot(rows=(_row(title="Fix login"),))
    assert asyncio.run(h.svc._publish_diff(rec, binding, board))
    assert calls["n"] == 2
    assert h.client.last("remove_view") == {"view_id": "V1"}
    assert rec.view_id == "" and rec.diff_hash == ""


def test_a_refused_board_takes_down_a_worker_tab_it_cannot_replace(tmp_path, repo, monkeypatch):
    """The channel's tab carries helper agents, the board is refused now, and no
    channel-only diff builds: the tab is taken down."""
    h = _Harness(tmp_path, snapshot=_secret_worker(tmp_path, repo, monkeypatch))
    binding = _FakeBinding(project_dir=str(repo))
    rec = asyncio.run(h.svc.ensure(binding))
    (Path(rec.worktree) / "README.md").write_text("channel change\n")
    asyncio.run(h.svc.refresh(binding))  # a combined tab goes out
    assert "secret_plan.txt" in h.client.last("set_diff_view")["content"]
    assert h.svc.store.get("T1", "C1").diff_workers
    monkeypatch.setattr(h.svc, "_build_diff", lambda record, board_arg=None, trees=None: None)
    source.set_scope_predicate(lambda state, b: False)  # shared since
    h.snapshot = None  # so the board read is refused
    asyncio.run(h.svc.refresh(binding))
    assert h.client.last("remove_view") == {"view_id": "V1"}
    assert not h.svc.store.get("T1", "C1").diff_workers
    # A channel that never had a board keeps its tab when the build fails.
    h2 = _Harness(tmp_path / "plain")
    other = _FakeBinding(channel_id="C2", project_dir=str(repo))
    asyncio.run(h2.svc.ensure(other))
    plain = h2.svc.store.get("T1", "C2")
    plain.view_id, plain.diff_hash = "V2", "old"
    h2.svc.store.put(plain)
    monkeypatch.setattr(h2.svc, "_build_diff", lambda record, board_arg=None, trees=None: None)
    asyncio.run(h2.svc.refresh(other))
    assert not any(m == "remove_view" for m, _kw in h2.client.calls)


def test_a_worker_tab_owed_before_a_restart_is_still_taken_down_after_it(
    tmp_path, repo, monkeypatch
):
    """GPT on ea16baea32: the owed takedown lived in memory, so a restart forgot it."""
    h = _Harness(tmp_path, snapshot=_secret_worker(tmp_path, repo, monkeypatch))
    binding = _FakeBinding(project_dir=str(repo))
    rec = asyncio.run(h.svc.ensure(binding))
    (Path(rec.worktree) / "README.md").write_text("channel change\n")
    asyncio.run(h.svc.refresh(binding))  # a combined tab goes out
    view_id = h.svc.store.get("T1", "C1").view_id
    assert view_id and h.svc.store.get("T1", "C1").diff_workers
    # A new gateway over the same store: the channel turned shared meanwhile,
    # and git cannot build anything yet.
    after = _Harness(tmp_path)
    after.snapshot = None
    source.set_scope_predicate(lambda state, b: False)
    monkeypatch.setattr(after.svc, "_build_diff", lambda record, board_arg=None, trees=None: None)
    asyncio.run(after.svc.refresh(binding))
    assert after.client.last("remove_view") == {"view_id": view_id}
    assert not after.svc.store.get("T1", "C1").diff_workers


def test_a_worker_tab_owed_across_a_rebind_is_still_taken_down(tmp_path, repo, monkeypatch):
    """GPT and Opus on 34ac95774f: a rebind kept the tab's view_id but not its flag."""
    h = _Harness(tmp_path, snapshot=_secret_worker(tmp_path, repo, monkeypatch))
    binding = _FakeBinding(project_dir=str(repo))
    rec = asyncio.run(h.svc.ensure(binding))
    (Path(rec.worktree) / "README.md").write_text("channel change\n")
    asyncio.run(h.svc.refresh(binding))  # a combined tab goes out
    view_id = h.svc.store.get("T1", "C1").view_id
    assert view_id and h.svc.store.get("T1", "C1").diff_workers
    stored = h.svc.store.get("T1", "C1")
    stored.worktree_skipped = service.REPAIRED  # the next turn start rebinds it
    h.svc.store.put(stored)
    asyncio.run(h.svc.ensure(binding))
    assert h.svc.store.get("T1", "C1").worktree_skipped != service.REPAIRED  # rebound
    assert h.svc.store.get("T1", "C1").diff_workers  # travels with the tab's view_id
    h.snapshot = None
    source.set_scope_predicate(lambda state, b: False)
    monkeypatch.setattr(h.svc, "_build_diff", lambda record, board_arg=None, trees=None: None)
    asyncio.run(h.svc.refresh(binding))
    assert h.client.last("remove_view") == {"view_id": view_id}


def test_an_unchanged_channel_diff_still_replaces_a_tab_whose_answer_was_lost(
    tmp_path, repo, monkeypatch
):
    """Opus and GPT on db4f1f35e4: the stored digest predated the lost send."""
    h = _Harness(tmp_path)
    binding = _FakeBinding(project_dir=str(repo))
    rec = asyncio.run(h.svc.ensure(binding))
    (Path(rec.worktree) / "README.md").write_text("channel change\n")
    asyncio.run(h.svc.refresh(binding))  # a channel-only tab lands (digest H0)
    sent = len([m for m, _kw in h.client.calls if m == "set_diff_view"])
    h.snapshot = _secret_worker(tmp_path, repo, monkeypatch)
    real = h.client.set_diff_view

    async def lost(channel_id, content, **kwargs):
        await real(channel_id, content, **kwargs)
        raise CodeChannelApiError(
            "agents.conversations.setView", "transport_error", maybe_applied=True
        )

    h.client.set_diff_view = lost
    try:
        asyncio.run(h.svc.refresh(binding))  # the combined tab, answer lost
        assert h.svc.store.get("T1", "C1").diff_workers
        h.client.set_diff_view = real
        h.snapshot = None
        source.set_scope_predicate(lambda state, b: False)
        asyncio.run(h.svc.refresh(binding))  # the channel's own diff is H0 again
        tabs = [kw["content"] for m, kw in h.client.calls if m == "set_diff_view"]
        assert len(tabs) == sent + 2 and "secret_plan.txt" not in tabs[-1]  # resent
        assert not h.svc.store.get("T1", "C1").diff_workers
    finally:
        board_pkg._timers.pop("C1", None)
        board_pkg._unsent.pop("C1", None)


def test_a_worker_tab_is_owed_on_disk_before_its_send(tmp_path, repo, monkeypatch):
    """GPT on db4f1f35e4: a pass cancelled after the send never wrote the flag down."""
    h = _Harness(tmp_path, snapshot=_secret_worker(tmp_path, repo, monkeypatch))
    binding = _FakeBinding(project_dir=str(repo))
    rec = asyncio.run(h.svc.ensure(binding))
    (Path(rec.worktree) / "README.md").write_text("channel change\n")
    real = h.client.set_diff_view

    async def applied_then_cancelled(channel_id, content, **kwargs):
        await real(channel_id, content, **kwargs)
        raise asyncio.CancelledError

    h.client.set_diff_view = applied_then_cancelled
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(h.svc.refresh(binding))
    board_pkg._timers.pop("C1", None)
    assert h.svc.store.get("T1", "C1").diff_workers  # a restart still owes it


def test_a_rebind_landing_while_a_takedown_is_answered_owes_nothing_more(
    tmp_path, repo, monkeypatch
):
    """GPT on db4f1f35e4: the rebound merge OR-ed back a takedown Slack had answered."""
    h = _Harness(tmp_path)
    binding = _FakeBinding(project_dir=str(repo))
    rec = asyncio.run(h.svc.ensure(binding))
    readme = Path(rec.worktree) / "README.md"
    committed = readme.read_bytes()  # the checkout's own bytes (CRLF on Windows)
    readme.write_text("changed\n", newline="\n")
    asyncio.run(h.svc.refresh(binding))  # a tab V1 goes out
    stored = h.svc.store.get("T1", "C1")
    stored.diff_workers = True  # as if it carried helper agents
    h.svc.store.put(stored)
    readme.write_bytes(committed)  # the tree goes clean: the tab is removed
    real_clear = h.svc._clear_diff

    async def clear_while_rebound(record):
        done = await real_clear(record)
        rebound = h.svc.store.get("T1", "C1")
        rebound.worktree, rebound.branch = "/elsewhere", "slack/other"
        h.svc.store.put(rebound)  # the rebind's record, still carrying V1 and the flag
        return done

    monkeypatch.setattr(h.svc, "_clear_diff", clear_while_rebound)
    asyncio.run(h.svc.refresh(binding))
    after = h.svc.store.get("T1", "C1")
    assert h.client.last("remove_view") == {"view_id": "V1"}
    assert after.view_id == "" and not after.diff_workers


def test_every_owed_record_outlives_the_store_cap(tmp_path, monkeypatch):
    """GPT on 37310b85e0: owed records beyond the cap were still dropped."""
    from kiro_crew.slack.code_channels.workspace import store as ws_store

    monkeypatch.setattr(ws_store, "MAX_RECORDS", 2)
    st = ws_store.WorkspaceStore(tmp_path / "ws.json")
    for n in range(3):
        st.put(ws_store.WorkspaceRecord("T1", f"C-owed{n}", view_id="V", diff_workers=True))
    for n in range(2):
        st.put(ws_store.WorkspaceRecord("T1", f"C{n}"))
    assert all(st.get("T1", f"C-owed{n}") is not None for n in range(3))
    assert st.get("T1", "C0") is None and st.get("T1", "C1") is None


def test_a_refused_send_undoes_its_write_ahead_even_across_a_rebind(tmp_path, repo, monkeypatch):
    """GPT on 37310b85e0: a rebind carried the write-ahead flag of a send Slack refused."""
    h = _Harness(tmp_path, snapshot=_secret_worker(tmp_path, repo, monkeypatch))
    binding = _FakeBinding(project_dir=str(repo))
    rec = asyncio.run(h.svc.ensure(binding))
    (Path(rec.worktree) / "README.md").write_text("channel change\n")

    async def rebound_then_refused(channel_id, content, **kwargs):
        rebound = h.svc.store.get("T1", "C1")
        assert rebound.diff_workers  # the write-ahead is on disk before the send
        rebound.worktree, rebound.branch = "/elsewhere", "slack/other"
        h.svc.store.put(rebound)  # a rebind lands, carrying the flag
        raise CodeChannelApiError("agents.conversations.setView", "ratelimited")

    h.client.set_diff_view = rebound_then_refused
    try:
        asyncio.run(h.svc.refresh(binding))
        assert not h.svc.store.get("T1", "C1").diff_workers  # nothing went out
    finally:
        board_pkg._timers.pop("C1", None)
        board_pkg._unsent.pop("C1", None)


def test_an_owed_record_outlives_the_store_cap(tmp_path, monkeypatch):
    from kiro_crew.slack.code_channels.workspace import store as ws_store

    monkeypatch.setattr(ws_store, "MAX_RECORDS", 3)
    # Windows' clock can stamp quick puts alike, so each put gets its own later time.
    clock = iter(range(1000, 2000))
    monkeypatch.setattr(ws_store, "time", SimpleNamespace(time=lambda: float(next(clock))))
    st = ws_store.WorkspaceStore(tmp_path / "ws.json")
    st.put(ws_store.WorkspaceRecord("T1", "C-owed", view_id="V1", diff_workers=True))
    for n in range(4):  # each one touched later than the owed record
        st.put(ws_store.WorkspaceRecord("T1", f"C{n}"))
    assert st.get("T1", "C-owed") is not None  # kept while it owes a takedown
    assert st.get("T1", "C0") is None and st.get("T1", "C3") is not None


def _lost_worker_tab(tmp_path, repo, monkeypatch):
    """A combined tab Slack applied but whose answer was lost: owed, id unknown."""
    h = _Harness(tmp_path, snapshot=_secret_worker(tmp_path, repo, monkeypatch))
    binding = _FakeBinding(project_dir=str(repo))
    rec = asyncio.run(h.svc.ensure(binding))
    (Path(rec.worktree) / "README.md").write_text("channel change\n")
    real = h.client.set_diff_view

    async def lost(channel_id, content, **kwargs):
        await real(channel_id, content, **kwargs)  # Slack applied it ...
        raise CodeChannelApiError(  # ... but the answer never came back
            "agents.conversations.setView", "transport_error", maybe_applied=True
        )

    h.client.set_diff_view = lost
    asyncio.run(h.svc.refresh(binding))
    stored = h.svc.store.get("T1", "C1")
    assert stored.view_id == "" and stored.diff_workers  # owed, id unknown
    board_pkg._timers.pop("C1", None)  # the lost send's own re-arm
    board_pkg._unsent.pop("C1", None)
    h.client.set_diff_view = real
    # The channel turns shared and has nothing of its own to show.
    h.snapshot = None
    source.set_scope_predicate(lambda state, b: False)
    _git("checkout", "--", "README.md", cwd=Path(rec.worktree))
    return h, binding


def test_a_worker_tab_whose_answer_was_lost_is_replaced_by_a_fixed_line_then_removed(
    tmp_path, repo, monkeypatch
):
    """GPT on 2f366a3e02; lead's ruling 1: the owed tab is withdrawn, never left spinning."""
    from kiro_crew.slack.code_channels.workspace import code_tab

    h, binding = _lost_worker_tab(tmp_path, repo, monkeypatch)
    try:
        asyncio.run(h.svc.refresh(binding))
        sent = [kw["content"] for m, kw in h.client.calls if m == "set_diff_view"]
        assert sent[-1] == code_tab.WITHDRAWN_TAB  # the singleton replaced by one fixed line
        assert h.client.last("remove_view") == {"view_id": "V1"}  # then removed by its id
        stored = h.svc.store.get("T1", "C1")
        assert stored.view_id == "" and not stored.diff_workers
        assert "C1" not in board_pkg._timers  # done: nothing re-armed
    finally:
        board_pkg._timers.pop("C1", None)
        board_pkg._unsent.pop("C1", None)


def test_a_withdrawal_slack_refuses_stays_owed_backs_off_and_tells_the_owner_once(
    tmp_path, repo, monkeypatch
):
    h, binding = _lost_worker_tab(tmp_path, repo, monkeypatch)
    told: list[str] = []
    monkeypatch.setattr(
        surface,
        "dashboard_state",
        lambda: SimpleNamespace(notify=lambda source_, title, body, meta=None: told.append(title)),
    )
    tries: list[str] = []

    async def refused(channel_id, content, **kwargs):
        tries.append(content)
        raise CodeChannelApiError("agents.conversations.setView", "invalid_arguments")

    h.client.set_diff_view = refused
    clock = {"now": 1000.0}  # pinned: real time must not run out the backoff
    h.svc._clock = lambda: clock["now"]
    try:
        asyncio.run(h.svc.refresh(binding))
        asyncio.run(h.svc.refresh(binding))  # inside the backoff: no second try
        assert len(tries) == 1 and told == ["Code tab not withdrawn"]
        assert h.svc.store.get("T1", "C1").diff_workers  # still owed
        assert "C1" in board_pkg._timers  # re-armed, under settle's backoff
        clock["now"] += 61  # the backoff ran out
        asyncio.run(h.svc.refresh(binding))
        assert len(tries) == 2 and told == ["Code tab not withdrawn"]  # told once
    finally:
        board_pkg._timers.pop("C1", None)
        board_pkg._unsent.pop("C1", None)


def test_an_owed_tab_waits_out_slack_off_then_comes_down_first(tmp_path, repo, monkeypatch):
    """Lead's ruling 2: kept on disk, not re-armed while off, taken down once back."""
    h = _Harness(tmp_path, snapshot=_secret_worker(tmp_path, repo, monkeypatch))
    binding = _FakeBinding(project_dir=str(repo))
    rec = asyncio.run(h.svc.ensure(binding))
    (Path(rec.worktree) / "README.md").write_text("channel change\n")
    asyncio.run(h.svc.refresh(binding))  # a combined tab V1
    stored = h.svc.store.get("T1", "C1")
    stored.slack_off = "not_authorized"  # Slack turned this channel's views off
    h.svc.store.put(stored)
    h.snapshot = None
    source.set_scope_predicate(lambda state, b: False)
    calls = len(h.client.calls)
    try:
        asyncio.run(h.svc.refresh(binding))
        assert len(h.client.calls) == calls  # nothing can be sent
        assert h.svc.store.get("T1", "C1").diff_workers and "C1" not in board_pkg._timers
        # Back: the rebind its next turn start makes clears slack_off, keeping the flag.
        stored = h.svc.store.get("T1", "C1")
        stored.worktree_skipped = service.REPAIRED
        h.svc.store.put(stored)
        asyncio.run(h.svc.ensure(binding))
        back = h.svc.store.get("T1", "C1")
        assert back.slack_off == "" and back.diff_workers
        asyncio.run(h.svc.refresh(binding))
        content = h.client.last("set_diff_view")["content"]
        assert "secret_plan.txt" not in content  # the channel's own tab replaced it
        assert not h.svc.store.get("T1", "C1").diff_workers
    finally:
        board_pkg._timers.pop("C1", None)
        board_pkg._unsent.pop("C1", None)


def test_switching_a_channel_off_takes_its_worker_tab_down_now(tmp_path, repo, monkeypatch):
    """Lead's ruling 3: Off or Review refuses the board, not Slack writes."""
    h = _Harness(tmp_path, snapshot=_secret_worker(tmp_path, repo, monkeypatch))
    binding = _FakeBinding(project_dir=str(repo))
    rec = asyncio.run(h.svc.ensure(binding))
    (Path(rec.worktree) / "README.md").write_text("channel change\n")
    asyncio.run(h.svc.refresh(binding))  # a combined tab V1
    assert h.svc.store.get("T1", "C1").diff_workers
    monkeypatch.setattr(service, "_activation_admits", lambda b: False)  # set to Off
    try:
        asyncio.run(h.svc.refresh(binding))
        assert h.client.last("remove_view") == {"view_id": "V1"}
        stored = h.svc.store.get("T1", "C1")
        assert stored.view_id == "" and not stored.diff_workers
        assert "C1" not in board_pkg._timers
    finally:
        board_pkg._timers.pop("C1", None)
        board_pkg._unsent.pop("C1", None)


def test_the_watch_arms_an_owed_takedown_after_a_restart_with_no_board_posted(
    tmp_path, repo, monkeypatch
):
    """GPT and Opus on a910c6331e: an On channel whose scope refuses it, idle, no board_ts."""
    h = _Harness(tmp_path)
    binding = _FakeBinding(project_dir=str(repo))
    asyncio.run(h.svc.ensure(binding))
    stored = h.svc.store.get("T1", "C1")
    stored.diff_workers, stored.view_id = True, "V1"  # the tab went out, the board did not
    h.svc.store.put(stored)
    source.set_scope_predicate(lambda state, b: False)  # shared since
    monkeypatch.setattr(service, "_activation_admits", lambda b: True)
    watched = SimpleNamespace(channel_id="C1", team_id="T1", session_key="chat-cond", archived_at=0)
    monkeypatch.setattr(service, "_bindings_by_channel", lambda: {"C1": watched})
    monkeypatch.setattr(service, "_archived", lambda record: False)
    armed: list[str] = []
    monkeypatch.setattr(h.svc, "schedule_refresh", lambda b, delay=0.0: armed.append(b.channel_id))
    asyncio.run(h.svc.watch_tick(SimpleNamespace(_slots={})))
    assert armed == ["C1"]
    stored = h.svc.store.get("T1", "C1")
    stored.slack_off = "not_authorized"  # nothing can be sent: not re-armed while off
    h.svc.store.put(stored)
    h.svc._takedown_retry.clear()
    asyncio.run(h.svc.watch_tick(SimpleNamespace(_slots={})))
    assert armed == ["C1"]


def test_the_watch_keeps_arming_an_off_channels_takedown_with_backoff(tmp_path, repo, monkeypatch):
    h = _Harness(tmp_path)
    binding = _FakeBinding(project_dir=str(repo))
    asyncio.run(h.svc.ensure(binding))
    stored = h.svc.store.get("T1", "C1")
    stored.diff_workers, stored.view_id = True, "V1"
    h.svc.store.put(stored)
    monkeypatch.setattr(service, "_activation_admits", lambda b: False)
    watched = SimpleNamespace(channel_id="C1", team_id="T1", session_key="chat-cond", archived_at=0)
    monkeypatch.setattr(service, "_bindings_by_channel", lambda: {"C1": watched})
    monkeypatch.setattr(service, "_archived", lambda record: False)
    armed: list[str] = []
    monkeypatch.setattr(h.svc, "schedule_refresh", lambda b, delay=0.0: armed.append(b.channel_id))
    clock = {"now": 1000.0}
    h.svc._clock = lambda: clock["now"]
    asyncio.run(h.svc.watch_tick(SimpleNamespace(_slots={})))
    asyncio.run(h.svc.watch_tick(SimpleNamespace(_slots={})))
    assert armed == ["C1"]  # armed, then not again inside the backoff
    clock["now"] += 61  # a broken retry chain is picked up again once it runs out
    asyncio.run(h.svc.watch_tick(SimpleNamespace(_slots={})))
    assert armed == ["C1", "C1"]
    stored = h.svc.store.get("T1", "C1")
    stored.diff_workers = False  # taken down
    h.svc.store.put(stored)
    clock["now"] += 1000
    asyncio.run(h.svc.watch_tick(SimpleNamespace(_slots={})))
    assert armed == ["C1", "C1"] and "T1:C1" not in h.svc._takedown_retry


def test_a_worker_tab_send_slack_refused_owes_nothing(tmp_path, repo, monkeypatch):
    """A send Slack answered with a refusal never landed: the tab up is unchanged."""
    h = _Harness(tmp_path, snapshot=_secret_worker(tmp_path, repo, monkeypatch))
    binding = _FakeBinding(project_dir=str(repo))
    rec = asyncio.run(h.svc.ensure(binding))
    (Path(rec.worktree) / "README.md").write_text("channel change\n")

    async def refused(channel_id, content, **kwargs):
        raise CodeChannelApiError("agents.conversations.setView", "ratelimited")

    h.client.set_diff_view = refused
    try:
        asyncio.run(h.svc.refresh(binding))
        assert not h.svc.store.get("T1", "C1").diff_workers
    finally:
        board_pkg._timers.pop("C1", None)
        board_pkg._unsent.pop("C1", None)


def test_a_worker_tab_owed_after_a_refused_read_is_retried_until_it_goes(
    tmp_path, repo, monkeypatch
):
    """GPT on bcf6443265: a refused read and a failed takedown left nothing owed."""
    h = _Harness(tmp_path, snapshot=_secret_worker(tmp_path, repo, monkeypatch))
    binding = _FakeBinding(project_dir=str(repo))
    rec = asyncio.run(h.svc.ensure(binding))
    (Path(rec.worktree) / "README.md").write_text("channel change\n")
    asyncio.run(h.svc.refresh(binding))  # a combined tab goes out
    h.snapshot = None  # the board is refused from now on
    source.set_scope_predicate(lambda state, b: False)
    monkeypatch.setattr(h.svc, "_build_diff", lambda record, board_arg=None, trees=None: None)
    real = h.client.remove_view
    failures = {"left": 1}

    async def flaky(channel_id, view_id):
        if failures["left"]:
            failures["left"] -= 1
            raise CodeChannelApiError("agents.conversations.removeView", "ratelimited")
        return await real(channel_id, view_id)

    h.client.remove_view = flaky
    try:
        asyncio.run(h.svc.refresh(binding))
        assert "C1" in board_pkg._timers  # owed: re-armed although no board was read
        assert h.svc.store.get("T1", "C1").diff_workers
        asyncio.run(h.svc.refresh(binding))  # the re-armed pass
        assert h.client.last("remove_view") == {"view_id": "V1"}
        stored = h.svc.store.get("T1", "C1")
        assert stored.view_id == "" and not stored.diff_workers
    finally:
        board_pkg._timers.pop("C1", None)
        board_pkg._unsent.pop("C1", None)


def test_a_diff_step_that_raises_with_a_board_leaves_the_pass_unsent(tmp_path, repo, monkeypatch):
    """GPT on bcf6443265: a git timeout in the build counted the worker tab as landed."""
    h = _Harness(tmp_path, snapshot=model.BoardSnapshot(rows=(_row(title="Fix login"),)))
    binding = _FakeBinding(project_dir=str(repo))
    asyncio.run(h.svc.ensure(binding))

    def timeout(record, board_arg=None, trees=None):
        raise RuntimeError("git timed out")

    monkeypatch.setattr(h.svc, "_build_diff", timeout)
    try:
        asyncio.run(h.svc.refresh(binding))
        assert len(h.published) == 1  # the board still goes out
        assert "T1:C1" not in h.svc._board_refreshed and "C1" in board_pkg._timers
    finally:
        board_pkg._timers.pop("C1", None)
        board_pkg._unsent.pop("C1", None)


def test_a_transient_build_failure_keeps_an_admitted_worker_tab(tmp_path, repo, monkeypatch):
    """Opus on bcf6443265: a build that returned nothing for a moment removed the tab."""
    h = _Harness(tmp_path, snapshot=_secret_worker(tmp_path, repo, monkeypatch))
    binding = _FakeBinding(project_dir=str(repo))
    rec = asyncio.run(h.svc.ensure(binding))
    (Path(rec.worktree) / "README.md").write_text("channel change\n")
    asyncio.run(h.svc.refresh(binding))
    monkeypatch.setattr(h.svc, "_build_diff", lambda record, board_arg=None, trees=None: None)
    try:
        asyncio.run(h.svc.refresh(binding))
        assert not any(m == "remove_view" for m, _kw in h.client.calls)
        # GPT on 145472dbc9: the tab is owed its rebuild, so the pass is re-armed.
        assert "C1" in board_pkg._timers
    finally:
        board_pkg._timers.pop("C1", None)
        board_pkg._unsent.pop("C1", None)


def test_a_failed_tab_removal_leaves_the_board_unsent_and_re_armed(tmp_path, repo):
    """Adversarial B1: a failed removeView counted as landed, so nothing was pending."""
    h = _Harness(tmp_path, snapshot=model.BoardSnapshot(rows=(_row(state=model.DONE),)))
    binding = _FakeBinding(project_dir=str(repo))
    asyncio.run(h.svc.ensure(binding))
    stored = h.svc.store.get("T1", "C1")
    stored.view_id, stored.diff_hash = "V1", "old"  # a combined tab is up; the diff is now empty
    h.svc.store.put(stored)

    async def refused(channel_id, view_id):
        raise CodeChannelApiError("agents.conversations.removeView", "ratelimited")

    h.client.remove_view = refused
    try:
        asyncio.run(h.svc.refresh(binding))
        assert h.svc.store.get("T1", "C1").view_id == "V1"  # the tab is still up
        assert "T1:C1" not in h.svc._board_refreshed  # so the pass is not counted as sent
        assert "C1" in board_pkg._timers and board_pkg._unsent["C1"] == 1  # one re-publish
    finally:
        board_pkg._timers.pop("C1", None)
        board_pkg._unsent.pop("C1", None)


def test_a_share_recorded_during_the_outbound_check_sends_no_worker_content(monkeypatch):
    """GPT: the board's scope was judged before the client's outbound await."""
    from kiro_crew.slack.code_channels.surface_client import SurfaceClient

    sent: list[str] = []

    async def api_call(method, **kw):
        sent.append(method)
        return {"ok": True, "view_id": "V1"}

    shared = {"now": False}

    async def outbound():
        shared["now"] = True  # Slack Connect: the share is recorded during this await
        return True

    monkeypatch.setattr(surface, "outbound_permitted", outbound)
    monkeypatch.setattr(surface, "channel_policy_refusal", lambda *a, **k: "")
    client = SurfaceClient(api_call)
    with pytest.raises(CodeChannelApiError) as tab:
        asyncio.run(
            client.set_diff_view("C1", "diff --git a/w b/w\n", gate=lambda: not shared["now"])
        )
    assert tab.value.code == SCOPE_REFUSED and sent == []

    def boom() -> bool:
        raise RuntimeError("scope unreadable")

    with pytest.raises(CodeChannelApiError):
        asyncio.run(client.set_diff_view("C1", "diff --git a/w b/w\n", gate=boom))  # fails closed
    asyncio.run(client.set_diff_view("C1", "diff --git a/w b/w\n", gate=None))  # no workers: sent
    assert sent == ["agents.conversations.setView"]


# ── per-row PR and CI ───────────────────────────────────────────────────────


def test_worker_pr_by_number_fallback_and_cache(tmp_path, repo):
    pr = PrLookup("found", PullRequest(7, _URL + "7", "merged", ""))
    snap = model.BoardSnapshot(rows=(_row("it_00000001", model.DONE, pr_number=7),))
    h = _Harness(tmp_path, snapshot=snap, prs={"#7": pr})
    h.svc._clock = lambda: 1000.0  # pinned: real time must not run out the PR cache
    binding = _FakeBinding(project_dir=str(repo))
    asyncio.run(h.svc.ensure(binding))
    asyncio.run(h.svc.refresh(binding))
    asyncio.run(h.svc.refresh(binding))
    assert h.pr_queries.count("#7") == 1  # a merged PR is read once, then cached
    assert h.published[-1].rows[0].pr_state == "merged"


def test_unconfirmed_pr_number_is_shown_without_a_link(tmp_path, repo):
    snap = model.BoardSnapshot(rows=(_row("it_00000001", model.WORKING, pr_number=99),))
    h = _Harness(tmp_path, snapshot=snap)
    binding = _FakeBinding(project_dir=str(repo))
    asyncio.run(h.svc.ensure(binding))
    asyncio.run(h.svc.refresh(binding))
    row = h.published[-1].rows[0]
    assert row.pr_number == 99 and row.pr_url == "" and row.pr_ci == ""
    assert "PR #99" in render.row_text(row, _link) and "<https" not in render.pr_words(row)


def test_worker_pr_cache_is_scoped_to_the_forge_host(tmp_path):
    from kiro_crew.slack.code_channels.workspace.remote import RepoRef

    h = _Harness(
        tmp_path,
        prs={"b": PrLookup("found", PullRequest(1, "https://github.com/a/r/pull/1", "open"))},
    )
    public = RepoRef(host="github.com", owner="a", name="r")
    enterprise = RepoRef(host="ghe.example.com", owner="a", name="r")
    asyncio.run(h.svc._worker_pr(public, "b", 0))
    asyncio.run(h.svc._worker_pr(enterprise, "b", 0))
    assert h.pr_queries == ["b", "b"]


def test_the_worker_pr_lookups_run_without_the_channel_lock(tmp_path, repo):
    snap = model.BoardSnapshot(rows=(_row("it_00000001", model.WORKING, branch="feat/x"),))
    h = _Harness(tmp_path, snapshot=snap)
    binding = _FakeBinding(project_dir=str(repo))
    asyncio.run(h.svc.ensure(binding))
    lock = h.svc._lock("T1:C1")
    held: list[bool] = []
    inner = h.svc._find_pr

    async def find_pr(repo_, branch):
        held.append(lock.locked())
        return await inner(repo_, branch)

    h.svc._find_pr = find_pr
    asyncio.run(h.svc.refresh(binding))
    # The channel's own PR and the worker's: both read off the lock.
    assert "feat/x" in h.pr_queries and held and not any(held)


def test_a_failing_worker_pr_read_never_keeps_the_board_from_going_out(tmp_path, repo, monkeypatch):
    snap = model.BoardSnapshot(rows=(_row("it_00000001", model.WORKING, branch="feat/x"),))
    h = _Harness(tmp_path, snapshot=snap)
    binding = _FakeBinding(project_dir=str(repo))
    asyncio.run(h.svc.ensure(binding))

    async def boom(record, board):
        raise RuntimeError("gh went away")

    monkeypatch.setattr(h.svc, "_fill_worker_prs", boom)
    asyncio.run(h.svc.refresh(binding))
    assert len(h.published) == 1 and h.published[0].rows[0].pr_number == 0


def test_a_reported_pr_number_wins_over_a_later_pr_on_a_reused_branch(tmp_path, repo):
    """GPT on b980216ae0: branch discovery picked open #22 over the worker's merged #11."""
    prs = {
        "#11": PrLookup("found", PullRequest(11, _URL + "11", "merged", "")),
        "feat/login": PrLookup("found", PullRequest(22, _URL + "22", "open", "running")),
    }
    snap = model.BoardSnapshot(
        rows=(_row("it_00000001", model.DONE, branch="feat/login", pr_number=11),)
    )
    h = _Harness(tmp_path, snapshot=snap, prs=prs)
    binding = _FakeBinding(project_dir=str(repo))
    asyncio.run(h.svc.ensure(binding))
    asyncio.run(h.svc.refresh(binding))
    row = h.published[-1].rows[0]
    assert row.pr_number == 11 and row.pr_state == "merged" and row.pr_url.endswith("/11")
    assert "feat/login" not in h.pr_queries[1:]  # the worker's branch is never searched


def test_a_branch_with_no_reported_number_is_still_discovered(tmp_path, repo):
    prs = {"feat/x": PrLookup("found", PullRequest(5, "https://github.com/a/b/pull/5", "open", ""))}
    snap = model.BoardSnapshot(rows=(_row("it_00000001", model.WORKING, branch="feat/x"),))
    h = _Harness(tmp_path, snapshot=snap, prs=prs)
    binding = _FakeBinding(project_dir=str(repo))
    asyncio.run(h.svc.ensure(binding))
    asyncio.run(h.svc.refresh(binding))
    assert h.published[-1].rows[0].pr_number == 5


def _two_repos(tmp_path, repo, monkeypatch):
    """The channel on acme/widgets; a worker whose session moved to acme/other."""
    other = tmp_path / "other"
    other.mkdir()
    _git("init", "-q", str(other), cwd=tmp_path)
    _git("remote", "add", "origin", "git@github.com:acme/other.git", cwd=other)
    w1 = _live_slot("chat-w1", project=str(other))
    monkeypatch.setattr(surface, "dashboard_state", lambda: _Slots(**{"chat-w1": w1}))

    def remote(cwd):
        if str(cwd).startswith(str(other)):
            return "git@github.com:acme/other.git"
        return "git@github.com:acme/widgets"

    monkeypatch.setattr(git_ops, "remote_url", remote)


def test_a_worker_in_another_repository_shows_none_of_its_pr_or_branch(tmp_path, repo, monkeypatch):
    """GPT: #12 in the worker's repository B was read from the channel's repository A."""
    _two_repos(tmp_path, repo, monkeypatch)
    prs = {"#12": PrLookup("found", PullRequest(12, _URL + "12", "open", "passed"))}
    snap = model.BoardSnapshot(
        rows=(_row("it_00000001", branch="feat/b", pr_number=12, session_key="chat-w1"),)
    )
    h = _Harness(tmp_path, snapshot=snap, prs=prs)
    binding = _FakeBinding(project_dir=str(repo))
    asyncio.run(h.svc.ensure(binding))
    asyncio.run(h.svc.refresh(binding))
    row = h.published[-1].rows[0]
    assert (row.pr_number, row.pr_url, row.branch) == (0, "", "")
    assert "#12" not in h.pr_queries and "feat/b" not in h.pr_queries[1:]


def test_a_worker_whose_origin_names_no_comparable_repository_shows_no_pr(
    tmp_path, repo, monkeypatch
):
    """GPT on bcf6443265: a nested-group origin parsed as none, so the channel's #12 showed."""
    nested = tmp_path / "nested"
    nested.mkdir()
    w1 = _live_slot("chat-w1", project=str(nested))
    monkeypatch.setattr(surface, "dashboard_state", lambda: _Slots(**{"chat-w1": w1}))

    def remote(cwd):
        if str(cwd).startswith(str(nested)):
            return "git@gitlab.com:team/subgroup/other.git"
        return "git@github.com:acme/widgets"

    monkeypatch.setattr(git_ops, "remote_url", remote)
    prs = {"#12": PrLookup("found", PullRequest(12, _URL + "12", "open", "passed"))}
    snap = model.BoardSnapshot(rows=(_row("it_00000001", pr_number=12, session_key="chat-w1"),))
    h = _Harness(tmp_path, snapshot=snap, prs=prs)
    binding = _FakeBinding(project_dir=str(repo))
    asyncio.run(h.svc.ensure(binding))
    asyncio.run(h.svc.refresh(binding))
    row = h.published[-1].rows[0]
    assert (row.pr_number, row.pr_url) == (0, "") and "#12" not in h.pr_queries


def test_a_worker_whose_origin_differs_only_in_case_keeps_its_pr(tmp_path, repo, monkeypatch):
    """GPT on ea16baea32: ``Acme/Widgets`` and ``acme/widgets`` are one GitHub repository."""
    w1 = _live_slot("chat-w1", project=str(tmp_path / "clone"))
    monkeypatch.setattr(surface, "dashboard_state", lambda: _Slots(**{"chat-w1": w1}))

    def remote(cwd):
        if str(cwd).startswith(str(tmp_path / "clone")):
            return "git@GitHub.com:Acme/Widgets.git"
        return "git@github.com:acme/widgets"

    monkeypatch.setattr(git_ops, "remote_url", remote)
    prs = {"#12": PrLookup("found", PullRequest(12, _URL + "12", "open", "passed"))}
    snap = model.BoardSnapshot(rows=(_row("it_00000001", pr_number=12, session_key="chat-w1"),))
    h = _Harness(tmp_path, snapshot=snap, prs=prs)
    binding = _FakeBinding(project_dir=str(repo))
    asyncio.run(h.svc.ensure(binding))
    asyncio.run(h.svc.refresh(binding))
    assert h.published[-1].rows[0].pr_url.endswith("/12") and "#12" in h.pr_queries


def test_a_worker_in_the_channels_repository_keeps_its_pr(tmp_path, repo, monkeypatch):
    w1 = _live_slot("chat-w1", project=str(repo))
    monkeypatch.setattr(surface, "dashboard_state", lambda: _Slots(**{"chat-w1": w1}))
    monkeypatch.setattr(git_ops, "remote_url", lambda cwd: "git@github.com:acme/widgets")
    prs = {"#12": PrLookup("found", PullRequest(12, _URL + "12", "open", "passed"))}
    snap = model.BoardSnapshot(rows=(_row("it_00000001", pr_number=12, session_key="chat-w1"),))
    h = _Harness(tmp_path, snapshot=snap, prs=prs)
    binding = _FakeBinding(project_dir=str(repo))
    asyncio.run(h.svc.ensure(binding))
    asyncio.run(h.svc.refresh(binding))
    assert h.published[-1].rows[0].pr_url.endswith("/12")


def test_the_plan_rows_beyond_the_first_twenty_get_their_pr(tmp_path, monkeypatch):
    rows = tuple(_row(f"it_{i:04x}", model.WORKING, branch=f"feat/b{i}") for i in range(30))
    pr = PrLookup("found", PullRequest(7, _URL + "7", "open", "passed"))
    h = _Harness(tmp_path, prs={"feat/b25": pr})
    repo_ref = SimpleNamespace(host="github.com", slug="acme/widgets", owner="acme", name="widgets")
    from kiro_crew.slack.code_channels.workspace import worker_prs

    monkeypatch.setattr(worker_prs, "parse_remote", lambda url: repo_ref)
    monkeypatch.setattr(git_ops, "remote_url", lambda cwd: "git@github.com:acme/widgets")
    monkeypatch.setattr(h.svc, "_checkout_dir", lambda record: str(tmp_path))
    record = SimpleNamespace(key="T1:C1", worktree=str(tmp_path))
    filled = asyncio.run(h.svc._fill_worker_prs(record, model.BoardSnapshot(rows=rows)))
    assert filled.rows[25].pr_number == 7 and "feat/b25" in h.pr_queries


def test_a_pr_read_by_number_is_built_on_the_channels_own_repository(monkeypatch):
    from kiro_crew.dashboard.source_providers import chip_refresh
    from kiro_crew.slack.code_channels.workspace import pr as pr_mod
    from kiro_crew.slack.code_channels.workspace.remote import RepoRef

    asked: list[str] = []
    answers: dict = {"status": {"state": "merged", "ci": "passed", "ciCounts": "3/3/0"}}

    async def fetch(url):
        asked.append(url)
        if isinstance(answers["status"], Exception):
            raise answers["status"]
        return answers["status"]

    monkeypatch.setattr(chip_refresh, "_fetch_check_status", fetch)
    repo_ref = RepoRef(host="github.com", owner="acme", name="widgets")
    found = asyncio.run(pr_mod.pull_request_by_number(repo_ref, 12))
    assert asked == [_URL + "12"] and found.status == "found"
    assert (found.pr.url, found.pr.state, found.pr.ci, found.pr.checks) == (
        _URL + "12",
        "merged",
        "passed",
        (3, 3, 0),
    )
    for bad in (0, -1, True, "12"):
        assert asyncio.run(pr_mod.pull_request_by_number(repo_ref, bad)).reason == "number"
    answers["status"] = {"state": "weird"}
    assert asyncio.run(pr_mod.pull_request_by_number(repo_ref, 12)).reason == "state"
    answers["status"] = RuntimeError("gh down")
    assert asyncio.run(pr_mod.pull_request_by_number(repo_ref, 12)).reason == "gh"
    monkeypatch.setattr(pr_mod, "_github_host_allowed", lambda host: False)
    assert asyncio.run(pr_mod.pull_request_by_number(repo_ref, 12)).reason == "host"


def test_a_board_read_outside_a_refresh_is_filled_from_the_channels_repository(tmp_path, repo):
    """The wrap-up's fallback read: ``filled`` reads the PRs as a refresh does."""
    pr = PrLookup("found", PullRequest(7, _URL + "7", "open", "passed"))
    h = _Harness(tmp_path, prs={"#7": pr})
    binding = _FakeBinding(project_dir=str(repo))
    asyncio.run(h.svc.ensure(binding))
    snap = model.BoardSnapshot(rows=(_row("it_00000001", model.WORKING, pr_number=7),))
    assert asyncio.run(h.svc.filled(binding, snap)).rows[0].pr_url == _URL + "7"
    unknown = _FakeBinding(channel_id="C9", project_dir=str(repo))
    assert asyncio.run(h.svc.filled(unknown, snap)) is snap  # no workspace record: as is


def test_ci_counts_reach_the_board_but_never_the_dashboard_chip_cache(monkeypatch):
    from kiro_crew.dashboard.source_providers import chip_refresh, chip_status
    from kiro_crew.slack.code_channels.workspace import pr as pr_mod

    assert pr_mod._checks({"ciCounts": "12/20/3"}) == (12, 20, 3)
    assert pr_mod._checks({"ciCounts": "12/20"}) is None and pr_mod._checks({}) is None
    assert pr_mod._checks({"ciCounts": "1/2/x"}) is None
    assert pr_mod._checks({"ciCounts": "12345/1/0"}) is None  # over 4 digits
    assert pr_mod._checks({"ciCounts": 7}) is None
    assert chip_refresh._CHIP_CI_COUNTS == "ciCounts"
    url = _URL + "99"

    async def fetch(_url):
        return {"state": "open", "ci": "passed", "ciCounts": "1/1/0"}

    monkeypatch.setattr(chip_refresh, "_fetch_check_status", fetch)
    asyncio.run(chip_refresh._refresh_check_status(url))
    cached = chip_status._check_cache.pop(url, None)
    assert cached is not None and "ciCounts" not in json.dumps(cached, default=str)
    assert "passed" in json.dumps(cached, default=str)


def test_a_rows_pr_and_ci_show_on_the_board_in_both_looks():
    live_pr = _row(
        "it_00000001",
        model.WORKING,
        title="Fix login",
        pr_number=12,
        pr_url=_URL + "12",
        pr_state="open",
        pr_ci="running",
        pr_checks=(4, 6, 2),
    )
    merged = _row("it_00000002", model.DONE, title="Docs", pr_number=9, pr_url="http://x/9")
    merged = merged.with_pr(9, "javascript:alert(1)", "merged", "passed")
    snap = model.BoardSnapshot(rows=(live_pr, merged))
    tasks = render.plan_block(snap, _link, now=suite._NOW)["tasks"]
    first = json.dumps(tasks[0])
    assert '"url": "' + _URL + '12"' in first and "CI 4/6 passing \\u00b7 2 running" in first
    assert tasks[0]["sources"] == [{"type": "url", "url": _URL + "12", "text": "PR #12"}]
    second = json.dumps(tasks[1])
    assert "javascript" not in second and " merged" in second and "sources" not in tasks[1]
    assert "CI" not in second  # a settled PR shows no CI
    text = render.row_text(live_pr, _link)
    assert f"<{_URL}12|PR #12> · CI 4/6 passing · 2 running" in text
    failing = live_pr.with_pr(12, _URL + "12", "open", "failed", (5, 6, 0))
    assert "CI 5/6 passing · 1 failing" in render.row_text(failing, _link)  # Opus note 1
    assert snap.aggregate.endswith(" · 1 PR open")


# ── the wrap-up's outcomes ─────────────────────────────────────────────────


def test_wrap_up_lists_each_workers_outcome():
    rows = [
        _row(
            "it_00000001",
            model.DONE,
            title="Fix login",
            pr_number=12,
            pr_url="https://github.com/a/b/pull/12",
            pr_state="merged",
        ),
        _row(
            "it_00000002",
            model.WORKING,
            title="Docs",
            pr_number=13,
            pr_url="https://github.com/a/b/pull/13",
            pr_state="open",
            pr_ci="failed",
        ),
        _row("it_00000003", model.CLOSED, title="Spike", closed_as="abandoned"),
        _row("it_00000004", model.LOST, title="Flaky <test>"),
    ]
    lines = [render.outcome_line(r) for r in rows]
    assert lines == [
        "• Fix login: <https://github.com/a/b/pull/12|PR #12> merged",
        "• Docs: <https://github.com/a/b/pull/13|PR #13> open · tests failing",
        "• Spike: abandoned",
        "• Flaky &lt;test&gt;: lost, no PR",
    ]
    blocks = wrapup.wrap_up_blocks("All done.", wrapup.Facts(workers=tuple(lines)), "C1")
    context = blocks[1]["elements"][0]["text"]
    assert context.startswith("*Helper agents*\n• Fix login") and "• Spike: abandoned" in context


def test_gather_facts_carries_worker_outcomes(monkeypatch):
    async def lines(binding):
        return ["• A: abandoned"]

    monkeypatch.setattr(board_pkg, "outcome_lines", lines)
    monkeypatch.setattr(wrapup, "_gather_facts_sync", lambda b: wrapup.Facts(branch="x"))
    facts = asyncio.run(wrapup.gather_facts(_binding()))
    assert facts.workers == ("• A: abandoned",) and facts.branch == "x"


def test_wrap_up_outcomes_keep_rejected_apart_and_fit_slacks_text_cap():
    rejected = _row(
        "it_00000001",
        model.CLOSED,
        title="Spike",
        closed_as="rejected",
        pr_number=9,
        pr_url="https://github.com/a/b/pull/9",
        pr_state="closed",
    )
    assert (
        render.outcome_line(rejected)
        == "• Spike: rejected, <https://github.com/a/b/pull/9|PR #9> closed"
    )
    many = tuple(f"• {'w' * 140} {i}" for i in range(40))
    context = wrapup.wrap_up_blocks("s", wrapup.Facts(workers=many), "C1")[1]["elements"][0]["text"]
    assert len(context) < 3000 and context.rstrip().endswith("more")


def test_the_wrap_up_shows_no_cached_outcomes_once_the_channel_turns_shared(monkeypatch):
    """The cached board may predate the channel turning shared: the wrap-up re-checks scope."""
    from kiro_crew.slack.code_channels.workspace import core as ws_core

    snap = model.BoardSnapshot(rows=(_row(state=model.DONE, title="Fix login", pr_number=12),))
    monkeypatch.setattr(
        ws_core, "service", lambda: SimpleNamespace(board=lambda t, c, session_key="": snap)
    )
    monkeypatch.setattr(surface, "dashboard_state", lambda: object())
    admitted = {"now": True}
    source.set_scope_predicate(lambda state, b: admitted["now"])
    assert asyncio.run(board_pkg.outcome_lines(_binding()))  # admitted: the cached rows
    admitted["now"] = False
    assert asyncio.run(board_pkg.outcome_lines(_binding())) == []


def test_the_wrap_up_reads_the_board_without_the_readers_side_effects(monkeypatch):
    from kiro_crew.slack.code_channels.workspace import core as workspace

    monkeypatch.setattr(workspace, "service", lambda: None)
    monkeypatch.setattr(surface, "dashboard_state", lambda: None)

    async def read_board(state, binding):
        return model.BoardSnapshot(rows=(_row(state=model.DONE, session_key="chat-w9"),))

    async def side_effects(binding):
        raise AssertionError("the full reader re-arms triggers and publishes the aggregate")

    monkeypatch.setattr(source, "read_board", read_board)
    monkeypatch.setattr(board_pkg, "read", side_effects)
    lines = asyncio.run(board_pkg.outcome_lines(_binding()))
    assert len(lines) == 1 and "chat-w9" not in board_pkg._worker_channels


def test_the_wrap_up_fallback_read_is_filled_like_a_refresh(monkeypatch):
    from kiro_crew.slack.code_channels.workspace import core as workspace

    raw = model.BoardSnapshot(rows=(_row(state=model.DONE, pr_number=12, session_key="chat-w1"),))
    cleaned = model.BoardSnapshot(rows=(_row(state=model.DONE, session_key="chat-w1"),))
    calls: list = []

    class Svc:
        def board(self, team, channel, session_key=""):
            return None

        async def filled(self, binding, snapshot):
            calls.append(snapshot)
            return cleaned

    monkeypatch.setattr(workspace, "service", lambda: Svc())
    monkeypatch.setattr(surface, "dashboard_state", lambda: None)

    async def read_board(state, binding):
        return raw

    monkeypatch.setattr(source, "read_board", read_board)
    lines = asyncio.run(board_pkg.outcome_lines(_binding()))
    assert calls == [raw] and not any("#12" in line for line in lines)


def test_the_wrap_up_reads_the_new_conductors_ledger_not_the_old_cache(tmp_path, monkeypatch):
    from kiro_crew.slack.code_channels.workspace import core as ws_core

    h = _Harness(tmp_path)
    rounds._cached_for(h, "chat-cond", title="Old worker")
    monkeypatch.setattr(ws_core, "service", lambda: h.svc)
    monkeypatch.setattr(surface, "dashboard_state", lambda: None)
    source.set_scope_predicate(lambda state, binding: True)
    fresh = model.BoardSnapshot(rows=(_row(state=model.DONE, title="New worker"),))
    read_for: list[str] = []

    async def read_board(state, binding):
        read_for.append(binding.session_key)
        return fresh

    monkeypatch.setattr(source, "read_board", read_board)
    lines = asyncio.run(board_pkg.outcome_lines(_binding(session="chat-cond-2")))
    assert read_for == ["chat-cond-2"]
    assert any("New worker" in line for line in lines)
    assert not any("Old worker" in line for line in lines)


def test_a_wrap_up_whose_scope_flips_before_the_post_leaves_the_workers_out(slack, monkeypatch):
    """GPT on 5da454ba22: the lock wait and the archive check can outlast a sharing change."""
    admitted = {"now": True}

    async def facts(binding):
        return wrapup.Facts(branch="feat/x", workers=("• Fix login: PR #12 merged",))

    async def archived_then_shared(binding):
        admitted["now"] = False  # Slack Connect recorded while the post waited
        return False

    async def no_store(channel, **fields):
        return None

    monkeypatch.setattr(wrapup, "gather_facts", facts)
    monkeypatch.setattr(surface, "is_archived", archived_then_shared)
    monkeypatch.setattr(surface, "channel_admits", lambda cfg, b: True)
    monkeypatch.setattr(surface, "audit", lambda *a, **k: None)
    monkeypatch.setattr(wrapup, "update_async", no_store)
    monkeypatch.setattr(surface, "dashboard_state", lambda: None)
    source.set_scope_predicate(lambda state, b: admitted["now"])
    posted: list = []

    async def post_blocks(channel, blocks, text, thread_ts=None):
        posted.append(json.dumps(blocks))
        return "300.1"

    monkeypatch.setattr(slack, "post_blocks", post_blocks)
    asyncio.run(wrapup.post_wrap_up(_binding(), "All done", caller="U0"))
    assert posted and "Fix login" not in posted[0] and "feat/x" in posted[0]


class _BlocksSlack:
    def __init__(self) -> None:
        self.edits: list[list[dict]] = []

    async def update_message(self, channel, ts, text="", blocks=None):
        self.edits.append(list(blocks or []))


def _wrap_up_with_workers():
    facts = wrapup.Facts(branch="feat/x", workers=("• Fix login: PR #12 merged",))
    return wrapup.wrap_up_blocks("Shipped it.", facts, "C1")


def test_a_wrap_up_re_send_leaves_the_outcome_lines_out_once_the_scope_refuses(monkeypatch):
    """Adversarial N1: the retire edit re-sent the helper agents' lines unchecked."""
    blocks = _wrap_up_with_workers()
    fake = _BlocksSlack()
    monkeypatch.setattr(wrapup, "_workers_still_admitted", lambda b: False)  # shared since
    asyncio.run(wrapup._retire_unrecorded(fake, "C1", "100.1", blocks, _binding()))
    [sent] = fake.edits
    text = json.dumps(sent)
    assert "Fix login" not in text and "Helper agents" not in text and "Shipped it." in text
    monkeypatch.setattr(wrapup, "_workers_still_admitted", lambda b: True)
    assert wrapup._shown(_binding(), blocks) == blocks  # in scope: unchanged


def test_a_stop_in_the_retire_tasks_first_loop_pass_sends_nothing(monkeypatch):
    """Adversarial N1: the Stop check ran before the shielded task even started."""
    blocks = _wrap_up_with_workers()
    fake = _BlocksSlack()
    monkeypatch.setattr(wrapup, "_workers_still_admitted", lambda b: True)

    async def go():
        token = surface.admit()
        try:
            asyncio.get_running_loop().call_soon(surface.mark_stopped, "C-retire")
            await wrapup._retire_unrecorded(fake, "C-retire", "100.1", blocks, _binding())
        finally:
            surface.release(token)

    try:
        asyncio.run(go())
    finally:
        surface._stopped_at.pop("C-retire", None)
    assert fake.edits == []


def test_the_sidebar_summary_comes_from_the_pr_filled_board(monkeypatch):
    """Opus: the aggregate must be built from the PR-filled board, so it says "PR open"."""
    earlier = {"state": "working", "label": "an earlier summary", "counts": {}}
    conductor = SimpleNamespace(key="chat-cond", _work_aggregate=earlier)
    state = SimpleNamespace(get_slot=lambda key: conductor if key == "chat-cond" else None)
    monkeypatch.setattr(surface, "dashboard_state", lambda: state)
    monkeypatch.setattr(links, "link_builder", lambda: _link)
    source.set_scope_predicate(lambda st, b: True)

    async def fake_publish(binding, snapshot, *, link_for, owner_id="", admits=None):
        return "updated"

    monkeypatch.setattr(publisher, "publish", fake_publish)
    filled = model.BoardSnapshot(rows=(_row(pr_number=12, pr_state="open"),))
    asyncio.run(board_pkg.publish(_binding(), filled))
    assert "PR open" in conductor._work_aggregate["label"]
