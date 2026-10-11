"""The worktree-create endpoint must not reuse a worktree whose ``git worktree add``
never finished.

``git worktree add`` registers the worktree, sets its HEAD and writes a ``locked``
= "initializing" marker BEFORE it checks files out, clearing the marker only once
the checkout finishes. If the add is killed in between (a gateway stop or restart
kills its control group), the entry is left registered, on its branch, partly
checked out and ``locked initializing``. A retry must not report that as a ready,
reused worktree, or the session opens on a tree missing most of its files and the
agent's first commit records them as deleted.

Scratch repositories under ``tmp_path`` only. The interrupted add is built directly
(``git worktree add --no-checkout``, then ``git worktree lock --reason initializing``),
so no process is killed and nothing depends on timing. The OS-sandbox wrapper is
replaced by a plain ``git`` call so the create path runs on any host.
"""

from __future__ import annotations

import os
import subprocess
import threading
from pathlib import Path

import pytest

from kiro_crew.dashboard.handlers import worktree as wt

_FILES = 4000


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", check=False
    )


@pytest.fixture
def repo(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(
        wt, "sandboxed_spawn_argv", lambda argv, **_k: (list(argv), dict(os.environ), None)
    )
    monkeypatch.setattr(wt, "config_hook_disable_args_sandboxed", lambda *_a, **_k: [])
    monkeypatch.setattr(wt, "is_sensitive_path", lambda _p: False)
    root = tmp_path / "proj" / "repo"
    root.mkdir(parents=True)
    # git canonicalizes a worktree's path on registration while the handler keys
    # reuse by the lexical dest, so on a host whose temp root contains a symlink
    # (``/home`` -> ``/local/home`` here) the two disagree. Realpath the root so
    # the handler, this helper and git all name the worktree the same way; the
    # defect under test is the lock check, not path canonicalization.
    root = Path(os.path.realpath(root))
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.invalid")
    _git(root, "config", "user.name", "t")
    _git(root, "config", "gc.auto", "0")
    for n in range(_FILES):
        (root / f"f{n:04d}.txt").write_text(f"file {n}\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    return root


def _dest_for(repo: Path, branch: str) -> Path:
    return repo.parent / f"{repo.name}-wt-{wt._dir_slug(branch)}"


def _files_in(dest: Path) -> int:
    return len(list(dest.glob("f*.txt")))


def _interrupt_add(repo: Path, branch: str) -> Path:
    """Leave the state a ``git worktree add`` killed mid-checkout leaves, every time.

    Registered, on its own branch, with its files not checked out, and ``locked``
    with the reason ``initializing``: git writes that marker before the checkout and
    clears it only after, and ``git worktree lock --reason`` writes the same file.
    """
    dest = _dest_for(repo, branch)
    added = _git(repo, "worktree", "add", "-q", "--no-checkout", "-b", branch, str(dest))
    assert added.returncode == 0, added.stderr
    locked = _git(repo, "worktree", "lock", "--reason", "initializing", str(dest))
    assert locked.returncode == 0, locked.stderr
    assert "locked initializing" in _git(repo, "worktree", "list", "--porcelain").stdout
    assert _files_in(dest) < _FILES
    return dest


def test_a_retry_after_an_interrupted_add_is_refused_not_reused(repo):
    dest = _interrupt_add(repo, "feat/cut")
    before = _files_in(dest)
    body, status = wt._create_worktree_sync(str(repo), "feat/cut")
    print(
        f"\nPROBE interrupted-add retry status={status} reused={body.get('reused')} "
        f"code={body.get('code')} files={before}/{_FILES}"
    )
    assert not (status == 200 and body.get("reused")), (
        f"a retry reported a worktree whose add never finished as ready: status {status}, "
        f"reused={body.get('reused')}, {before} of {_FILES} files, git lists it 'locked initializing'"
    )
    assert status == 409 and body.get("code") == "worktree_unfinished", (status, body)
    # The refusal touches nothing: the entry is still registered and still locked.
    assert "locked initializing" in _git(repo, "worktree", "list", "--porcelain").stdout


def test_a_completed_add_is_still_reused(repo):
    first, status = wt._create_worktree_sync(str(repo), "feat/done")
    assert status == 200 and not first["reused"]
    body, status = wt._create_worktree_sync(str(repo), "feat/done")
    assert status == 200 and body["reused"] is True, (status, body)
    assert _files_in(Path(body["path"])) == _FILES


def test_a_completed_worktree_locked_for_another_reason_is_still_reused(repo):
    first, status = wt._create_worktree_sync(str(repo), "feat/held")
    assert status == 200
    dest = Path(first["path"])
    assert _git(repo, "worktree", "lock", "--reason", "held by me", str(dest)).returncode == 0
    body, status = wt._create_worktree_sync(str(repo), "feat/held")
    assert status == 200 and body["reused"] is True, (status, body)
    assert Path(body["path"]) == dest
    # Reused as it is: still locked, still fully checked out.
    assert "locked held by me" in _git(repo, "worktree", "list", "--porcelain").stdout
    assert _files_in(dest) == _FILES


def _race(fn, args_list):
    barrier = threading.Barrier(len(args_list))
    results: list = [None] * len(args_list)

    def run(i, args):
        barrier.wait()
        results[i] = fn(*args)

    threads = [threading.Thread(target=run, args=(i, a)) for i, a in enumerate(args_list)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    return results


def test_same_branch_race_still_leaves_one_worktree(repo):
    results = _race(wt._create_worktree_sync, [(str(repo), "feat/same")] * 8)
    created = [b for b, s in results if s == 200 and not b.get("reused")]
    assert len(created) == 1, results
    winner = created[0]
    assert _files_in(Path(winner["path"])) == _FILES
    assert _git(repo, "worktree", "list", "--porcelain").stdout.count("worktree ") == 2


def test_cleanup_prune_race_keeps_every_live_worktree(repo, monkeypatch):
    real = wt._run_git
    failing = {f"feat/b{n}" for n in range(0, 8, 2)}

    def run_git(args, cwd, **kw):
        if args[:2] == ["worktree", "add"] and args[-1] in failing:
            return subprocess.CompletedProcess(args, 128, "", "fatal: injected failure")
        return real(args, cwd, **kw)

    monkeypatch.setattr(wt, "_run_git", run_git)
    results = _race(wt._create_worktree_sync, [(str(repo), f"feat/b{n}") for n in range(8)])
    ok = {b["branch"]: Path(b["path"]) for b, s in results if s == 200}
    assert set(ok) == {f"feat/b{n}" for n in range(1, 8, 2)}, sorted(ok)
    for branch, dest in ok.items():
        assert _files_in(dest) == _FILES, f"{branch}: damaged by a neighbour's cleanup"
        assert f"branch refs/heads/{branch}" in _git(repo, "worktree", "list", "--porcelain").stdout
