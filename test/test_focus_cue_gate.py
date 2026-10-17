"""Tests for ``scripts/check_focus_cue.py``.

The gate builds a real git repository in a temp directory so its diff parser is
driven against a hunk header git actually produced. That probe repository is the
one part of the gate that touches the filesystem and spawns git, so it is where a
cleanup crash can take the whole gate — and the ``Fast Gate`` it runs inside —
down on a diff already judged clean. These tests exercise the probe path directly:
the whole self-test contract, the config that keeps git from writing in the
background, and that a populated probe repository removes without raising.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPT_PATH = os.path.join(_REPO_ROOT, "scripts", "check_focus_cue.py")


def _load():
    spec = importlib.util.spec_from_file_location("check_focus_cue", _SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_focus_cue"] = module
    spec.loader.exec_module(module)
    return module


gate = _load()


def _git(repo: str, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    ).stdout


def _build_probe_repo(repo: str) -> None:
    """Drive a repo through the same steps the gate's probe uses, including the
    auto-maintenance-off config, then leave a commit behind."""
    os.makedirs(repo, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "probe@example.invalid")
    _git(repo, "config", "user.name", "probe")
    _git(repo, "config", "gc.auto", "0")
    _git(repo, "config", "maintenance.auto", "false")
    target = os.path.join(repo, "a.tsx")
    with open(target, "w", encoding="utf-8") as handle:
        handle.write('<input className="outline-none focus-visible:ring-accent" />\n')
    _git(repo, "add", "a.tsx")
    _git(repo, "commit", "-qm", "base")


class TestSelfTest:
    def test_self_test_passes_and_does_not_raise(self) -> None:
        # Runs the gate end to end, which builds the probe repository in a
        # TemporaryDirectory and lets that context manager remove it. A cleanup
        # that raced git's background repack would surface here as an OSError
        # rather than a return code.
        assert gate.self_test() == 0


class TestProbeRepoCleanup:
    def test_probe_repo_disables_background_maintenance(self) -> None:
        # The fix keeps git from writing into the probe repo after the foreground
        # command returns; both triggers must read as off so nothing repacks into
        # .git/objects/pack while the directory is being removed.
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "r")
            _build_probe_repo(repo)
            assert _git(repo, "config", "--get", "gc.auto").strip() == "0"
            assert _git(repo, "config", "--get", "maintenance.auto").strip() == "false"

    def test_populated_probe_repo_removes_cleanly(self, tmp_path) -> None:
        # A probe repo carries real objects under .git, so its removal is the
        # operation that failed with ENOTEMPTY when a background writer was live.
        # With maintenance off the tree is quiescent and its removal completes.
        # git writes loose objects read-only (mode 444), and Windows refuses to
        # unlink a read-only file, so a bare shutil.rmtree raises PermissionError
        # there. rmtree_force clears the read-only bit and retries, and returns a
        # filesystem-derived True only once the tree is actually gone -- it does
        # not swallow a failure into a false success, so a genuine leftover would
        # still fail this assertion rather than hide. The repo lives under
        # pytest's tmp_path: a passing run leaves nothing behind, and a failing
        # one keeps the tree on purpose (tmp_path_retention_policy = failed) for
        # inspection, cleaned by pytest on a later green run.
        from kiro_crew.platform_compat import rmtree_force

        repo = os.path.join(tmp_path, "r")
        _build_probe_repo(repo)
        pack_dir = os.path.join(repo, ".git", "objects")
        assert os.path.isdir(pack_dir)
        assert rmtree_force(repo) is True
        assert not os.path.exists(repo)

    def test_gate_source_sets_maintenance_off_before_commit(self) -> None:
        # Lock the fix in place at the source level: the probe's first commit must
        # be preceded by turning both maintenance triggers off, so a future edit
        # that drops the config (and reopens the race) fails this test.
        with open(_SCRIPT_PATH, encoding="utf-8") as handle:
            source = handle.read()
        gc_off = source.index('run("config", "gc.auto", "0")')
        maint_off = source.index('run("config", "maintenance.auto", "false")')
        first_commit = source.index('run("commit", "-qm", "base")')
        assert gc_off < first_commit
        assert maint_off < first_commit
