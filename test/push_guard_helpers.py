"""Shared repos, runners and fixtures for the push_guard.py tests.

``test_push_guard.py`` and its ``test_push_guard_*.py`` siblings import from here:
the script's path and loader (``_PG`` is one loaded copy for constants and pure
helpers; a run loads a fresh one), the bounded runners (``_run_push_guard`` as a
child, ``_inprocess`` in this process), the fixture git environment and ``_git``,
the ``repo_pair`` fixture and its session template (built once per session in each
module that binds it), the commit builders
(``_commit_tree``, ``_feature``, ``_land_on_main``, ``_bless``), the squash message
helpers, the ``Popen`` spy and the hook installers. ``_no_inherited_git_c`` is
autouse, so every test module binds it.
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from git_maintenance_helpers import GIT_MAINTENANCE_OFF_CONFIG, git_maintenance_off_entries
from installer_test_helpers import run_bounded
from skill_script_helpers import load_skill_script

from kiro_crew.platform.update_governance import _GIT_LOCATION_VARS

# Resolve the push_guard.py script path relative to the repo root.
REPO_ROOT = Path(__file__).resolve().parent.parent
PUSH_GUARD_DIR = (
    REPO_ROOT
    / "src"
    / "kiro_crew"
    / "builtin_skills"
    / "kirocrew-dev"
    / "kirocrew-prepare-pr"
    / "scripts"
)
PUSH_GUARD = str(PUSH_GUARD_DIR / "push_guard.py")


def _load_push_guard():
    """A private copy of push_guard.py: not in sys.modules, no sys.path entry."""
    return load_skill_script("push_guard_under_test", PUSH_GUARD)


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- the autouse fixture of test_push_guard.py and the modules split out of it, moved verbatim; none of them calls monkeypatch.undo()
def _no_inherited_git_c(monkeypatch):
    """The guard honours its caller's ``git -c`` (GIT_CONFIG_PARAMETERS) by design.

    A test is not that caller: an ancestor's ``-c core.hooksPath`` would run
    host hooks inside the run.  A test that needs the channel sets it itself.
    """
    monkeypatch.delenv("GIT_CONFIG_PARAMETERS", raising=False)


# Well under the per-test ``--timeout``, so a hung git fails the test by name
# instead of costing the worker (testing-conventions, determinism class 6).
_SUBPROCESS_TIMEOUT_S = 30
# The guard's own bounds in an in-process run, for the same reason.
_INPROCESS_TIMEOUT_S = 20


def _run_push_guard(
    cwd: str, extra_args: list[str] | None = None, env_extra: dict[str, str] | None = None
) -> tuple[int, str, str]:
    """Run push_guard.py in the given directory; return (rc, stdout, stderr).

    Bounded, and on a timeout the guard's whole process tree is reaped
    (``run_bounded``). The environment passes through as is -- including the
    rootdir conftest's ``GIT_CEILING_DIRECTORIES`` fence -- because the guard
    scrubs the variables that would retarget git itself.
    """
    args = [sys.executable, PUSH_GUARD, "--base", "main", *(extra_args or [])]
    proc = run_bounded(
        args, {**os.environ, **(env_extra or {})}, timeout=_SUBPROCESS_TIMEOUT_S, cwd=cwd
    )
    return proc.returncode, proc.stdout, proc.stderr


def _fixture_git_env() -> dict[str, str]:
    """Env for a fixture git call: no host config, templates, hooks, or identity bleed.

    The session/module-scoped template builders below run BEFORE the function-scoped
    ``_git_identity`` autouse fixture in ``test/conftest.py`` has pinned anything, so
    they would otherwise read the developer's real ``~/.gitconfig`` -- a
    ``commit.gpgSign`` aborts the whole template, and a ``core.hooksPath`` or
    ``init.templateDir`` would EXECUTE host hooks from inside the test run. The
    ``GIT_DIR`` location family is dropped (the production list, so an exported
    ``GIT_DIR`` from a hook or ``rebase --exec`` cannot retarget the fixture), both
    template channels are emptied, and identity is supplied. Deliberately NOT
    ``git_command_env()``: that pins ``diff.external`` empty for commands that never
    diff, and these fixtures run ``git diff``.
    """
    # GIT_CONFIG_PARAMETERS is an ancestor's ``git -c``: it would load a host
    # core.hooksPath past the file channels emptied below.
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in _GIT_LOCATION_VARS and k != "GIT_CONFIG_PARAMETERS"
    }
    env.update(
        {
            "GIT_TEMPLATE_DIR": "",
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "init.templateDir",
            "GIT_CONFIG_VALUE_0": "",
        }
    )
    # Last, so the entry lands after the GIT_CONFIG_COUNT this helper set.
    env.update(git_maintenance_off_entries(env))
    return env


def _git(cwd: str, *args: str) -> str:
    """Run a git command in cwd with the scrubbed fixture env; raise on failure."""
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        env=_fixture_git_env(),
        timeout=_SUBPROCESS_TIMEOUT_S,
    )
    return proc.stdout.strip()


@pytest.fixture(scope="session")
def _repo_pair_template(tmp_path_factory) -> tuple[str, str]:
    """Build the bare origin + initial clone once per session; ``repo_pair`` copies it.

    Six git subprocesses (~1-1.6s) would otherwise be paid on every one of the ~40
    tests below. Session scope is safe because the template directories are
    never handed to a test, only copied from via ``shutil.copytree`` -- so a
    test that pushes, branches, or clones ``work2`` off its own copy of
    ``origin_dir`` cannot reach another test's copy.
    """
    root = tmp_path_factory.mktemp("push-guard-seed")
    origin_dir = str(root / "origin.git")
    clone_dir = str(root / "work")

    os.makedirs(origin_dir)
    _git(origin_dir, "init", "--bare")
    _git(origin_dir, "symbolic-ref", "HEAD", "refs/heads/main")
    # A push starts receive-pack here without our env (see git_maintenance_helpers).
    for key, value in GIT_MAINTENANCE_OFF_CONFIG:
        _git(origin_dir, "config", key, value)

    _git(str(root), "clone", origin_dir, "work")
    _git(clone_dir, "checkout", "-b", "main")

    Path(clone_dir, "README.md").write_text("initial\n")
    _git(clone_dir, "add", "README.md")
    _git(clone_dir, "commit", "-m", "initial commit")
    _git(clone_dir, "push", "-u", "origin", "main")

    return clone_dir, origin_dir


@pytest.fixture
def repo_pair(tmp_path, _repo_pair_template):
    """A local 'origin' bare repo and a working clone, copied from the template.

    Returns (clone_dir, origin_dir) where origin_dir is a bare repo and
    clone_dir has 'origin' pointing at origin_dir. Each test gets its own copy,
    so pushes, branches, and `work2` clones (which several tests create
    alongside this pair) never touch another test's copy.
    """
    template_clone, template_origin = _repo_pair_template
    origin_dir = str(tmp_path / "origin.git")
    clone_dir = str(tmp_path / "work")
    shutil.copytree(template_origin, origin_dir)
    shutil.copytree(template_clone, clone_dir)
    # The copied clone's remote still points at the TEMPLATE's origin path;
    # repoint it at this test's own copy so pushes/fetches never reach (or
    # mutate) the session template or another test's copy.
    _git(clone_dir, "remote", "set-url", "origin", origin_dir)
    # copytree resets every file's mtime, which invalidates git's cached
    # index stat info and makes git see a false "unstaged changes" diff (e.g.
    # git rebase refuses with "You have unstaged changes"). Nothing in the
    # template is ever uncommitted, so resetting hard to HEAD is a no-op on
    # content and forces git to re-stat every file against the real index.
    _git(clone_dir, "reset", "--hard", "HEAD")
    return clone_dir, origin_dir


def _commit_tree(clone_dir: str, files: dict[str, str | None], message: str) -> None:
    """Write (or, for ``None``, delete) each file, stage everything, commit.

    Off ``main`` this is a commit of the branch's own, so its paths enter the
    guard's build record as ``--commit`` would leave them. A commit made BY
    HAND (the provenance tests) uses ``_git(..., "commit")`` directly.
    """
    for rel, content in files.items():
        path = Path(clone_dir, rel)
        if content is None:
            path.unlink()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
    _git(clone_dir, "add", "-A")
    _git(clone_dir, "commit", "-q", "-m", message)
    on_main = _git(clone_dir, "symbolic-ref", "-q", "HEAD") == "refs/heads/main"
    if not on_main and _git(clone_dir, "rev-list", "--count", "HEAD") != "1":
        _bless(clone_dir, "HEAD^")


def _bless(clone_dir: str, since: str = "refs/remotes/origin/main") -> None:
    """Record every path HEAD changes against ``since`` as the guard's own.

    For tests whose subject is another check, on commits made with plain git:
    this is the record ``--commit`` would have left for HEAD.
    """
    paths = _git(clone_dir, "diff-tree", "-r", "--name-only", "--no-renames", since, "HEAD")
    git_dir = _git(clone_dir, "rev-parse", "--absolute-git-dir")
    _PG.record_built(git_dir, _rev(clone_dir, "HEAD"), {p.encode() for p in paths.split()})


def _land_on_main(clone_dir: str, files: dict[str, str | None], message: str) -> None:
    """Commit on main and push it, the way a merged upstream PR lands."""
    _git(clone_dir, "checkout", "-q", "main")
    _commit_tree(clone_dir, files, message)
    _git(clone_dir, "push", "-q", "origin", "main")


def _rev(repo: str, rev: str) -> str:
    return _git(repo, "rev-parse", "--verify", rev)


def _feature(clone_dir: str, files: dict[str, str | None] | None = None) -> str:
    """A feature branch with one commit of its own; returns its HEAD."""
    _git(clone_dir, "checkout", "-q", "-b", "feature/x")
    _commit_tree(clone_dir, files or {"mine.py": "# mine\n"}, "feat: mine")
    return _rev(clone_dir, "HEAD")


_PG = _load_push_guard()  # constants and pure helpers only; runs use a fresh copy


def _message_path(clone_dir: str) -> str:
    """The checked-out branch's default squash message file."""
    return _PG.squash_message_path(
        _git(clone_dir, "rev-parse", "--absolute-git-dir"),
        _git(clone_dir, "symbolic-ref", "--short", "HEAD"),
    )


def _write_message(clone_dir: str, text: str = "feat: the squashed change\n\nDetail.\n") -> str:
    """The squash message at its default home, inside git's own directory."""
    path = _message_path(clone_dir)
    Path(path).write_text(text, encoding="utf-8")
    return path


def _spy_popen(monkeypatch, on_init=None, on_wait=None):
    """Wrap subprocess.Popen for an in-process guard run.

    ``on_init(argv, kwargs)`` runs before each spawn; ``on_wait(argv, timeout)``
    before each wait and may raise (a simulated hang).
    """
    real = subprocess.Popen

    class _Popen(real):  # type: ignore[misc, valid-type]
        def __init__(self, args, *a, **kw):
            if on_init:
                on_init(list(args), kw)
            super().__init__(args, *a, **kw)

        def wait(self, timeout=None):
            if on_wait:
                on_wait(list(self.args), timeout)
            return super().wait(timeout)

    monkeypatch.setattr(subprocess, "Popen", _Popen)


def _run_push_guard_inprocess(
    monkeypatch,
    cwd: str,
    fake_git_cmd: list[str] | None = None,
    *,
    extra_args: list[str] | None = None,
    overrides: dict[str, object] | None = None,
) -> tuple[int, str, str]:
    """Run push_guard.main() in-process with monkeypatched _GIT_CMD.

    Args:
        monkeypatch: pytest monkeypatch fixture.
        cwd: Working directory for the guard (the git repo).
        fake_git_cmd: Command list for push_guard._GIT_CMD, or None
            to use real git.
        extra_args: Arguments after ``--base main``.
        overrides: Module attributes to set on this run's copy.

    The guard's bounds are pinned well under the suite's per-test timeout,
    so a hang fails this test by name instead of costing the worker.

    Returns:
        (exit_code, stdout, stderr) — mirrors subprocess interface.
    """
    # A private copy per run: no shared module state between tests.
    push_guard = _load_push_guard()

    # Monkeypatch the git command injection point.
    if fake_git_cmd is not None:
        monkeypatch.setattr(push_guard, "_GIT_CMD", fake_git_cmd)
    monkeypatch.setattr(push_guard, "GIT_TIMEOUT_S", _INPROCESS_TIMEOUT_S)
    monkeypatch.setattr(push_guard, "HOOK_TIMEOUT_S", _INPROCESS_TIMEOUT_S)
    monkeypatch.setattr(push_guard, "FETCH_STALL_S", _INPROCESS_TIMEOUT_S)
    for name, value in (overrides or {}).items():
        monkeypatch.setattr(push_guard, name, value)
    # Monkeypatch argv for argparse.
    monkeypatch.setattr(sys, "argv", ["push_guard.py", "--base", "main", *(extra_args or [])])

    # Change working directory to the test repo.
    monkeypatch.chdir(cwd)

    # Capture stdout and stderr.
    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout_buf)
    monkeypatch.setattr(sys, "stderr", stderr_buf)

    # Run main(); it returns an exit code (does not call sys.exit).
    exit_code = push_guard.main()

    return exit_code, stdout_buf.getvalue(), stderr_buf.getvalue()


_inprocess = _run_push_guard_inprocess


def _overlay(clone_dir: str) -> str:
    """Main moves README on; an older main is checked out over this tree (the incident)."""
    old_main = _rev(clone_dir, "HEAD")
    _land_on_main(clone_dir, {"README.md": "v2\n"}, "docs: readme")
    head = _feature(clone_dir)
    _git(clone_dir, "checkout", old_main, "--", ".")
    return head


def _install_hooks(clone_dir: str, tmp_path: Path, **bodies: str) -> Path:
    """sh hooks by keyword (``commit_msg`` is ``commit-msg``), wired by core.hooksPath."""
    hooks = tmp_path / "hooks"
    hooks.mkdir(exist_ok=True)
    for name, body in bodies.items():
        hook = hooks / name.replace("_", "-")
        hook.write_text("#!/bin/sh\n" + body)
        hook.chmod(0o755)
    _git(clone_dir, "config", "core.hooksPath", str(hooks))
    return hooks


def _run_from(cwd: str, args: list[str]) -> tuple[int, str, str]:
    """push_guard.py run from any directory of the checkout; bounded."""
    proc = run_bounded(
        [sys.executable, PUSH_GUARD, "--base", "main", *args],
        dict(os.environ),
        timeout=_SUBPROCESS_TIMEOUT_S,
        cwd=cwd,
    )
    return proc.returncode, proc.stdout, proc.stderr
