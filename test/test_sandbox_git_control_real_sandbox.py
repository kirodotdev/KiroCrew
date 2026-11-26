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
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from kiro_crew import sandbox
from kiro_crew import sandbox_git_control as gc
from kiro_crew.config.loader_locations import config_path
from kiro_crew.sandbox import userns_available
from kiro_crew.testing.wait import default_timeout

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
    root = (tmp_path / "ro" / "ws" / "checkout").resolve()
    root.mkdir(parents=True)
    _git("init", "-q", cwd=root)
    (root / "README.md").write_text("x\n", encoding="utf-8")
    _git("add", "README.md", cwd=root)
    _git("commit", "-qm", "init", cwd=root)
    monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(root))
    # Only the temporary repository: the source checkout running the test is left alone.
    monkeypatch.setattr(gc, "_source_checkout_root", lambda: None)
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
        timeout=default_timeout(),
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


def test_a_symlinked_config_is_held_in_place_and_its_target_sealed(home, repo, tmp_path):
    """``.git/config`` as a link: the agent can neither write through it nor swap it.

    The seal covers the link's target. The link sits in the writable git directory, so
    without a hold on the link itself the agent would unlink it and write its own
    ``config`` at the name the gateway's git reads.
    """
    config = repo / ".git" / "config"
    target = (tmp_path / "shared").resolve() / "config"
    target.parent.mkdir()
    target.write_bytes(config.read_bytes())
    config.unlink()
    config.symlink_to(target)
    argv = [sys.executable, "-c", _CHILD, str(repo), shutil.which("git") or "git"]
    env = {k: v for k, v in os.environ.items() if k != "KIROCREW_SANDBOX_ACTIVE"}
    proc = subprocess.run(
        sandbox.namespace_argv(argv, "standard"),
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=default_timeout(),
    )
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, output
    (line,) = [ln for ln in proc.stdout.splitlines() if ln.startswith("REPORT ")]
    report = json.loads(line[len("REPORT ") :])
    for step in ("config_abs", "config_rel"):
        assert report[step] == "EROFS", (step, report)
    for step in ("replace_config", "move_config"):
        assert report[step] == "EBUSY", (step, report)
    assert report["git_config"] != 0, report
    assert config.is_symlink() and os.readlink(config) == str(target)
    assert "fsmonitor" not in target.read_text(encoding="utf-8")
    # The control: git still reads its config through the held link.
    assert report["commit"] == 0, report


_EXCEPTION_CHILD = r"""
import errno, json, os, subprocess, sys

root, git, *paths = sys.argv[1:]
os.chdir(root)  # by name, as an agent's shell would

def attempt(step):
    try:
        step()
        return "ok"
    except OSError as exc:
        return errno.errorcode.get(exc.errno, str(exc.errno))

def append(path):
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("written")

def run(*args):
    proc = subprocess.run(
        [git, "-c", "user.name=t", "-c", "user.email=t@t", *args],
        capture_output=True, text=True,
    )
    return proc.returncode

report = {path: attempt(lambda: append(path)) for path in paths}
report["move_gitdir"] = attempt(lambda: os.rename(".git", ".git.old"))
report["commit"] = run("add", "work.txt") or run("commit", "-qm", "under an exception")
print("REPORT " + json.dumps(report), flush=True)
"""

#: Where the operator's write exception sits, as a path below the read-only directory,
#: and whether it leaves the checkout's own files writable.
_EXCEPTIONS = {
    "above the checkout": (("ws",), True),
    "the checkout and a sealed directory": (("ws/checkout", "ws/checkout/.git/hooks"), True),
    "a subfolder of the checkout": (("ws/checkout/sub",), False),
}


@pytest.mark.parametrize("case", list(_EXCEPTIONS))
def test_an_operator_exception_keeps_the_checkout_writable_and_its_git_files_sealed(
    home, repo, caplog, case
):
    """A write exception that holds the checkout is honoured, and re-opens no seal.

    The checkout sits in an operator read-only directory. An exception above it, or
    naming the checkout, leaves its files and ordinary commits working, as before the
    seals existed; one naming a subfolder opens only that subfolder. In every case, a
    sealed directory named outright included, the git control files stay read-only,
    ``.git`` cannot be moved aside and the rest of the read-only directory stays so.
    """
    exceptions, writable = _EXCEPTIONS[case]
    readonly = repo.parent.parent
    sub = repo / "sub"
    sub.mkdir()
    (repo / ".git" / "hooks").mkdir(exist_ok=True)
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "agent": {
                    "sandbox_readonly_paths": [str(readonly)],
                    "sandbox_readonly_exceptions": [str(readonly / rel) for rel in exceptions],
                }
            }
        ),
        encoding="utf-8",
    )
    config = repo / ".git" / "config"
    config_before = config.read_bytes()
    hook = repo / ".git" / "hooks" / "post-checkout"
    work, inside, outside = repo / "work.txt", sub / "work.txt", readonly / "outside.txt"
    targets = [work, inside, outside, config, hook]
    argv = [sys.executable, "-c", _EXCEPTION_CHILD, str(repo), shutil.which("git") or "git"]
    env = {k: v for k, v in os.environ.items() if k != "KIROCREW_SANDBOX_ACTIVE"}
    with caplog.at_level(logging.WARNING, logger="kiro_crew.sandbox"):
        launcher = sandbox.namespace_argv([*argv, *map(str, targets)], "standard")
    refusals = [
        r.getMessage()
        for r in caplog.records
        if r.name == "kiro_crew.sandbox" and "sandbox_readonly_exceptions" in r.getMessage()
    ]
    assert refusals == []
    proc = subprocess.run(
        launcher,
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=default_timeout(),
    )
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, output
    (line,) = [ln for ln in proc.stdout.splitlines() if ln.startswith("REPORT ")]
    report = json.loads(line[len("REPORT ") :])

    # The seals hold whatever the exception names ...
    for sealed in (config, hook, outside):
        assert report[str(sealed)] == "EROFS", (sealed, report)
    assert config.read_bytes() == config_before
    assert not hook.exists() and not outside.exists()
    assert (repo / ".git").is_dir() and not (repo / ".git.old").exists()
    # ... and the exception opens what it names: the control.
    assert report[str(inside)] == "ok", report
    assert inside.read_text(encoding="utf-8") == "written"
    if writable:
        assert report[str(work)] == "ok" and report["commit"] == 0, report
        assert report["move_gitdir"] == "EBUSY", report
        assert "under an exception" in _git("log", "-1", "--format=%s", cwd=repo)
    else:
        assert report[str(work)] == "EROFS" and report["commit"] != 0, report
        assert report["move_gitdir"] in ("EBUSY", "EROFS"), report
        assert not work.exists()
