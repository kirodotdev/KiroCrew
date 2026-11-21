"""The gateway checkout's git control files, seen from inside the product's own sandbox.

The test process plays the gateway: it points ``KIROCREW_PROJECT_DIR`` at a fresh
repository, plans a real namespace sandbox, and runs a child in it from the repository
root. The child tries every way to change what the gateway's own ``git`` would execute
there -- write ``.git/config`` by absolute and by relative path, drop a hook, write
``info/attributes``, move ``.git`` or the checkout aside -- and then makes an ordinary
commit and branch. The fence holds when every attack failed, the host's files are
unchanged, and the commit landed. The commit is the control: a child that could write
nothing at all would pass the rest.

Linux needs unprivileged user and mount namespaces, which the namespace-sandbox CI job
provides and the shard runners do not, so there these tests skip with that reason.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from kiro_crew import sandbox
from kiro_crew import sandbox_git_control as gc
from kiro_crew.sandbox import userns_available

pytestmark = [
    pytest.mark.skipif(sys.platform != "linux", reason="the namespace launcher is Linux-only"),
    pytest.mark.skipif(
        not userns_available(),
        reason="this host cannot build the namespace sandbox (no unprivileged user and mount "
        "namespaces)",
    ),
    pytest.mark.skipif(shutil.which("git") is None, reason="needs git"),
]

TIERS = ("standard", "cc", "strict")

_CHILD = r"""
import errno, json, os, subprocess, sys

root, git = sys.argv[1:3]
os.chdir(root)  # by name, as an agent's shell would

def attempt(step):
    try:
        step()
        return "ok"
    except OSError as exc:
        return errno.errorcode.get(exc.errno, str(exc.errno))

def append(path):
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("\n[core]\n\tfsmonitor = /tmp/evil\n")

def create(path):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("#!/bin/sh\necho pwned\n")

def run(*args):
    proc = subprocess.run(
        [git, "-c", "user.name=t", "-c", "user.email=t@t", *args],
        capture_output=True, text=True,
    )
    return proc.returncode

report = {
    "mnt_ns": os.readlink("/proc/self/ns/mnt"),
    "config_abs": attempt(lambda: append(os.path.join(root, ".git", "config"))),
    "config_rel": attempt(lambda: append(".git/config")),
    "hook": attempt(lambda: create(os.path.join(root, ".git", "hooks", "post-checkout"))),
    "attributes": attempt(lambda: create(".git/info/attributes")),
    "replace_config": attempt(lambda: os.replace(".git/description", ".git/config")),
    "move_config": attempt(lambda: os.rename(".git/config", ".git/config.old")),
    "move_hooks": attempt(lambda: os.rename(".git/hooks", ".git/hooks.old")),
    "move_gitdir": attempt(lambda: os.rename(".git", ".git.old")),
    "move_root": attempt(lambda: os.rename(root, root + ".old")),
    "git_config": run("config", "core.fsmonitor", "/tmp/evil"),
    "control": attempt(lambda: create("work.txt")),
    "add": run("add", "work.txt"),
    "commit": run("commit", "-qm", "from the sandbox"),
    "branch": run("branch", "from-sandbox"),
    "status": run("status", "--short"),
    "worktree": run("worktree", "add", "-q", "--no-track", root + "-wt", "-b", "wt-from-sandbox"),
}
print("REPORT " + json.dumps(report), flush=True)
"""


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        [
            "git",
            "-c",
            "init.defaultBranch=main",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            *args,
        ],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    home = (tmp_path / "home").resolve()
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    monkeypatch.setenv("KIROCREW_ASSUME_GATEWAY_RUNNING", "0")
    sandbox.reset_backend()
    yield home
    sandbox.reset_backend()


@pytest.fixture
def repo(tmp_path, monkeypatch) -> Path:
    root = (tmp_path / "ws" / "checkout").resolve()
    root.mkdir(parents=True)
    _git("init", "-q", cwd=root)
    (root / "README.md").write_text("x\n", encoding="utf-8")
    _git("add", "README.md", cwd=root)
    _git("commit", "-qm", "init", cwd=root)
    monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(root))
    # Only the temporary repository: the source checkout running the test is left alone.
    monkeypatch.setattr(gc, "_own_source_checkout", lambda: None)
    return root


@pytest.mark.parametrize("tier", TIERS)
def test_a_sandboxed_agent_cannot_change_what_host_git_runs(home, repo, tier):
    gitdir = repo / ".git"
    config_before = (gitdir / "config").read_bytes()
    argv = [sys.executable, "-c", _CHILD, str(repo), shutil.which("git") or "git"]
    env = {k: v for k, v in os.environ.items() if k != "KIROCREW_SANDBOX_ACTIVE"}
    proc = subprocess.run(
        sandbox.namespace_argv(argv, tier),
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, output
    (line,) = [ln for ln in proc.stdout.splitlines() if ln.startswith("REPORT ")]
    report = json.loads(line[len("REPORT ") :])
    assert report["mnt_ns"] != os.readlink("/proc/self/ns/mnt"), output

    # Every write to a control file is refused by the read-only seal ...
    for step in ("config_abs", "config_rel", "hook", "attributes"):
        assert report[step] == "EROFS", (step, report)
    # ... a seal cannot be replaced or moved, and neither can the dirs above it.
    for step in (
        "replace_config",
        "move_config",
        "move_hooks",
        "move_gitdir",
        "move_root",
    ):
        assert report[step] == "EBUSY", (step, report)
    assert report["git_config"] != 0, report

    # The host's files are what they were.
    assert (gitdir / "config").read_bytes() == config_before
    assert not (gitdir / "hooks" / "post-checkout").exists()
    assert not (gitdir / "info" / "attributes").exists()
    assert not (repo.parent / "checkout.old").exists()

    # And ordinary git still works inside: the control.
    assert report["control"] == "ok", report
    for step in ("add", "commit", "branch", "status", "worktree"):
        assert report[step] == 0, (step, report)
    assert "from the sandbox" in _git("log", "-1", "--format=%s", cwd=repo)
    assert "from-sandbox" in _git("branch", "--list", "from-sandbox", cwd=repo)
