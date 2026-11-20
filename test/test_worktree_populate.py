"""The hermetic worktree populate (``kiro_crew.worktree_populate``), against real git.

``git worktree add <dest> <ref>`` checks files out under the repository's own config, so a
``filter.<name>.smudge`` driver written there between a probe and the add runs. Both
callers (the release-channel create and the dashboard follow-up card) now add with
``--no-checkout`` and fill the tree with ``populate_worktree``, which never reads that config.

Three layers:

* **The threat**: each test that claims a driver does not run first shows the plain
  ``git worktree add`` DOES run it on the same repository, so a fixture that never armed
  the driver cannot pass for a fix.
* **The behaviour change, stated per setting**: a Git LFS file lands as its pointer, and
  line endings, symlinks and sparse-checkout come out as a plain ``worktree add`` makes them.
* **The callers**: the release create and the dashboard create, with the filter planted
  AFTER the probe answered clean and BEFORE the add, and the unwinds when the populate fails.

The git here is direct (not sandboxed): the strict-tier spawn that carries the real
populate (``private_dir`` as a window in the scratch mask) needs an OS sandbox backend a CI
runner may lack, so it is proven by hand on a host that has one and recorded in the pull
request.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from git_maintenance_helpers import git_maintenance_off_entries

from kiro_crew import agent_scratch, worktree_populate
from kiro_crew.apps.builtins.dev_fleet import release_worktree, repository, runtime
from kiro_crew.dashboard.handlers import worktree as dash_worktree
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")

_NO_CODE = ("-c", f"core.hooksPath={os.devnull}", "-c", "core.fsmonitor=false")

#: What git stores for a Git LFS file. A checkout with the LFS filter installed replaces it
#: with the real content; without a driver, the file stays this text.
_LFS_POINTER = (
    "version https://git-lfs.github.com/spec/v1\n"
    "oid sha256:4d7a214614ab2935c943f9e0ff69d22eadbb8f32b1258daaa5e2ca24d17e2393\n"
    "size 12\n"
)


def _env(home: Path) -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@e.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@e.invalid",
    }
    env.update(git_maintenance_off_entries(env))
    return env


def _git(env: dict[str, str], cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=FALLBACK_TIMEOUT_SECS,
    )


def _ok(proc: subprocess.CompletedProcess) -> str:
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


@pytest.fixture
def env(tmp_path) -> dict[str, str]:
    home = tmp_path / "home"
    home.mkdir()
    return _env(home)


def _run_git_for(env: dict[str, str]):
    """The caller's ``run_git``: the same ``-c`` pins ``_run_git`` applies, and the
    caller's own ``env`` when it passes one (the populate does), without the sandbox."""

    def run_git(args, cwd, **kw):
        return _git(kw.get("env") or env, Path(cwd), *_NO_CODE, *args)

    return run_git


def _make_repo(
    root: Path,
    env: dict[str, str],
    files: dict[str, str],
    *,
    attributes: str | None = None,
    links: dict[str, str] | None = None,
    init: tuple[str, ...] = (),
) -> tuple[Path, str]:
    """A repo at *root* with *files*, optional ``.gitattributes`` and symlinks (made with
    ``update-index`` so no OS symlink privilege is needed). Returns ``(root, commit)``."""
    root.mkdir()
    _ok(_git(env, root, "init", "-q", *init))
    for name, text in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode("utf-8"))
    if attributes is not None:
        (root / ".gitattributes").write_text(attributes, encoding="utf-8")
    _ok(_git(env, root, "add", "-A"))
    for name, target_text in (links or {}).items():
        blob = subprocess.run(
            ["git", "hash-object", "-w", "--stdin"],
            cwd=str(root),
            env=env,
            input=target_text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=FALLBACK_TIMEOUT_SECS,
        ).stdout.strip()
        _ok(_git(env, root, "update-index", "--add", "--cacheinfo", f"120000,{blob},{name}"))
    _ok(_git(env, root, "commit", "-qm", "one"))
    return root, _ok(_git(env, root, "rev-parse", "HEAD"))


def _snapshot(tree: Path) -> dict[str, tuple[str, Any]]:
    """Every path under *tree* but ``.git``: files by bytes, symlinks by target."""
    seen: dict[str, tuple[str, Any]] = {}
    for dirpath, dirnames, filenames in os.walk(tree):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for name in filenames + [d for d in dirnames if os.path.islink(os.path.join(dirpath, d))]:
            path = Path(dirpath) / name
            rel = path.relative_to(tree).as_posix()
            if rel == ".git":
                continue
            seen[rel] = (
                ("link", os.readlink(path)) if path.is_symlink() else ("file", path.read_bytes())
            )
    return seen


def _driver(marker: Path, body: str) -> str:
    """A filter command that records that it ran, then prints *body* (or its input)."""
    out = f"printf %s {shlex.quote(body)}" if body else "cat"
    script = f"printf ran > {shlex.quote(marker.as_posix())}; {out}"
    return f"sh -c {shlex.quote(script)}"


def _populated(env: dict[str, str], repo: Path, oid: str, dest: Path) -> None:
    """Add *dest* with ``--no-checkout`` and fill it with the hermetic populate."""
    _ok(_git(env, repo, "worktree", "add", "-q", "--no-checkout", "--detach", str(dest), oid))
    done = worktree_populate.populate_worktree(_run_git_for(env), str(dest), oid)
    assert done.returncode == 0, done.stderr


def _plain(env: dict[str, str], repo: Path, oid: str, dest: Path) -> None:
    """Add *dest* with one plain ``worktree add``, which checks out under the repo config."""
    _ok(_git(env, repo, "worktree", "add", "-q", "--detach", str(dest), oid))


# --------------------------------------------------------------------------
# a filter driver never runs
# --------------------------------------------------------------------------
def test_a_filter_driver_in_the_repo_config_never_runs(tmp_path, env):
    repo, oid = _make_repo(
        tmp_path / "repo",
        env,
        {"payload.txt": "release payload\n"},
        attributes="*.txt filter=evil\n",
    )
    marker = tmp_path / "filter-ran"
    _ok(_git(env, repo, "config", "filter.evil.smudge", _driver(marker, "")))
    _ok(_git(env, repo, "config", "filter.evil.required", "true"))

    _plain(env, repo, oid, tmp_path / "control")
    assert marker.exists(), "fixture never armed the driver: the plain checkout did not run it"
    marker.unlink()

    _populated(env, repo, oid, tmp_path / "populated")
    assert not marker.exists(), "the repository's filter driver ran during the populate"
    assert (tmp_path / "populated" / "payload.txt").read_bytes() == b"release payload\n"


def test_a_filter_driver_in_an_included_file_never_runs(tmp_path, env):
    """``include.path`` is part of the repo config the populate does not read."""
    repo, oid = _make_repo(
        tmp_path / "repo", env, {"payload.txt": "x\n"}, attributes="payload.txt filter=evil\n"
    )
    marker = tmp_path / "filter-ran"
    included = tmp_path / "hostile.cfg"
    included.write_text(f'[filter "evil"]\n\tsmudge = "{_driver(marker, "")}"\n', encoding="utf-8")
    _ok(_git(env, repo, "config", "include.path", included.as_posix()))

    _plain(env, repo, oid, tmp_path / "control")
    assert marker.exists(), "fixture never armed the driver"
    marker.unlink()

    _populated(env, repo, oid, tmp_path / "populated")
    assert not marker.exists()


def test_a_lfs_tracked_file_lands_as_its_pointer(tmp_path, env):
    """THE BEHAVIOUR CHANGE for Git LFS. The LFS filter is a driver, and a populate that
    consults no config defines none, so an LFS file stays the pointer text git stores. A
    plain ``worktree add`` on a host with LFS installed would have written the content."""
    repo, oid = _make_repo(
        tmp_path / "repo",
        env,
        {"model.bin": _LFS_POINTER, "readme.txt": "plain\n"},
        attributes="*.bin filter=lfs diff=lfs merge=lfs -text\n",
    )
    marker = tmp_path / "lfs-ran"
    _ok(_git(env, repo, "config", "filter.lfs.smudge", _driver(marker, "REAL LFS CONTENT")))
    _ok(_git(env, repo, "config", "filter.lfs.required", "true"))

    _plain(env, repo, oid, tmp_path / "control")
    assert (tmp_path / "control" / "model.bin").read_bytes() == b"REAL LFS CONTENT"
    marker.unlink()

    _populated(env, repo, oid, tmp_path / "populated")
    assert not marker.exists()
    assert (tmp_path / "populated" / "model.bin").read_text(encoding="utf-8") == _LFS_POINTER
    assert (tmp_path / "populated" / "readme.txt").read_bytes() == b"plain\n"


# --------------------------------------------------------------------------
# the allowlisted settings behave as a plain checkout makes them
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("config", "attributes", "expected"),
    [
        (("core.autocrlf", "true"), None, b"one\r\ntwo\r\n"),
        (("core.autocrlf", "input"), None, b"one\ntwo\n"),
        (("core.autocrlf", "false"), None, b"one\ntwo\n"),
        (("core.eol", "crlf"), "*.txt text\n", b"one\r\ntwo\r\n"),
        (("core.eol", "lf"), "*.txt text\n", b"one\ntwo\n"),
    ],
)
def test_line_endings_follow_core_autocrlf_and_core_eol(
    tmp_path, env, config, attributes, expected
):
    repo, oid = _make_repo(tmp_path / "repo", env, {"a.txt": "one\ntwo\n"}, attributes=attributes)
    _ok(_git(env, repo, "config", *config))

    _plain(env, repo, oid, tmp_path / "control")
    _populated(env, repo, oid, tmp_path / "populated")

    assert (tmp_path / "populated" / "a.txt").read_bytes() == expected
    assert _snapshot(tmp_path / "populated") == _snapshot(tmp_path / "control")


@pytest.mark.skipif(sys.platform == "win32", reason="a symlink needs a privilege Windows lacks")
def test_symlinks_are_created_when_core_symlinks_is_true(tmp_path, env):
    repo, oid = _make_repo(tmp_path / "repo", env, {"a.txt": "hi\n"}, links={"link": "a.txt"})
    _ok(_git(env, repo, "config", "core.symlinks", "true"))

    _plain(env, repo, oid, tmp_path / "control")
    _populated(env, repo, oid, tmp_path / "populated")

    assert os.readlink(tmp_path / "populated" / "link") == "a.txt"
    assert _snapshot(tmp_path / "populated") == _snapshot(tmp_path / "control")


def test_a_symlink_is_a_plain_file_when_core_symlinks_is_false(tmp_path, env):
    repo, oid = _make_repo(tmp_path / "repo", env, {"a.txt": "hi\n"}, links={"link": "a.txt"})
    _ok(_git(env, repo, "config", "core.symlinks", "false"))

    _plain(env, repo, oid, tmp_path / "control")
    _populated(env, repo, oid, tmp_path / "populated")

    link = tmp_path / "populated" / "link"
    assert not link.is_symlink() and link.read_bytes() == b"a.txt"
    assert _snapshot(tmp_path / "populated") == _snapshot(tmp_path / "control")


def _sparse_repo(tmp_path: Path, env: dict[str, str]) -> tuple[Path, str]:
    repo, oid = _make_repo(
        tmp_path / "repo", env, {"keep/a.txt": "1\n", "drop/b.txt": "2\n", "top.txt": "3\n"}
    )
    _ok(_git(env, repo, "sparse-checkout", "set", "--cone", "keep"))
    return repo, oid


def test_a_sparse_checkout_is_kept(tmp_path, env):
    repo, oid = _sparse_repo(tmp_path, env)

    _plain(env, repo, oid, tmp_path / "control")
    _populated(env, repo, oid, tmp_path / "populated")

    assert not (tmp_path / "populated" / "drop").exists()
    assert _snapshot(tmp_path / "populated") == _snapshot(tmp_path / "control")
    assert sorted(_snapshot(tmp_path / "populated")) == ["keep/a.txt", "top.txt"]


def test_a_sparse_checkout_with_no_readable_patterns_is_a_full_checkout(tmp_path, env):
    """Sparse with patterns the populate cannot read must not become a sparse tree with no
    patterns: it drops sparse and checks everything out."""
    repo, oid = _sparse_repo(tmp_path, env)
    dest = tmp_path / "populated"
    _ok(_git(env, repo, "worktree", "add", "-q", "--no-checkout", "--detach", str(dest), oid))
    git_dir = Path(_ok(_git(env, dest, "rev-parse", "--absolute-git-dir")))
    (git_dir / "info" / "sparse-checkout").unlink()

    done = worktree_populate.populate_worktree(_run_git_for(env), str(dest), oid)

    assert done.returncode == 0, done.stderr
    assert sorted(_snapshot(dest)) == ["drop/b.txt", "keep/a.txt", "top.txt"]


@pytest.mark.skipif(sys.platform == "win32", reason="a symlink needs a privilege Windows lacks")
def test_a_pattern_file_swapped_for_a_link_to_a_sensitive_file_is_not_read(
    tmp_path, env, monkeypatch
):
    """The pattern file sits where an agent can write. A link put in its place that resolves
    to a sensitive path is refused: read, its target's patterns would make this a sparse tree."""
    aws = tmp_path / "home" / ".aws"
    aws.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    repo, oid = _sparse_repo(tmp_path, env)
    dest = tmp_path / "populated"
    _ok(_git(env, repo, "worktree", "add", "-q", "--no-checkout", "--detach", str(dest), oid))
    patterns = (
        Path(_ok(_git(env, dest, "rev-parse", "--absolute-git-dir"))) / "info" / "sparse-checkout"
    )
    target = aws / "credentials"
    target.write_text("/*\n!/*/\n/keep/\n", encoding="utf-8")
    patterns.unlink()
    patterns.symlink_to(target)

    done = worktree_populate.populate_worktree(_run_git_for(env), str(dest), oid)

    assert done.returncode == 0, done.stderr
    assert sorted(_snapshot(dest)) == ["drop/b.txt", "keep/a.txt", "top.txt"]


def test_a_sha256_repository_is_populated(tmp_path, env):
    probe = _git(env, tmp_path, "init", "-q", "--object-format=sha256", str(tmp_path / "probe"))
    if probe.returncode != 0:
        pytest.skip("this git cannot make a sha256 repository")
    repo, oid = _make_repo(
        tmp_path / "repo", env, {"a.txt": "hi\n"}, init=("--object-format=sha256",)
    )
    assert len(oid) == 64

    _populated(env, repo, oid, tmp_path / "populated")

    assert (tmp_path / "populated" / "a.txt").read_bytes() == b"hi\n"
    assert _ok(_git(env, tmp_path / "populated", "status", "--porcelain")) == ""


def test_the_populated_tree_is_clean_and_matches_a_plain_checkout(tmp_path, env):
    repo, oid = _make_repo(
        tmp_path / "repo", env, {"a.txt": "one\n", "sub/b.txt": "two\n"}, attributes="*.txt text\n"
    )
    _plain(env, repo, oid, tmp_path / "control")
    _populated(env, repo, oid, tmp_path / "populated")
    populated = tmp_path / "populated"
    assert _ok(_git(env, populated, "status", "--porcelain")) == ""
    assert _ok(_git(env, populated, "rev-parse", "HEAD")) == oid
    assert _snapshot(populated) == _snapshot(tmp_path / "control")


# --------------------------------------------------------------------------
# what the populate hands git
# --------------------------------------------------------------------------
def test_allowed_settings_keeps_only_closed_values_and_the_last_assignment():
    listing = "\0".join(
        [
            "core.autocrlf\ntrue",
            "core.autocrlf\nInput",
            'core.eol\ncrlf\n[filter "x"]\n\tsmudge = evil',  # a value that carries a section
            "core.symlinks\nYES",
            "core.ignorecase\nmaybe",
            "core.sparsecheckout\n1",
            "core.sparsecheckoutcone\noff",
            "core.hookspath\n/tmp/hooks",  # not on the list
            "",
        ]
    )
    assert worktree_populate.allowed_settings(listing) == {
        "core.autocrlf": "input",
        "core.symlinks": "true",
        "core.sparsecheckout": "true",
        "core.sparsecheckoutcone": "false",
    }


def test_the_throwaway_git_dir_and_environment_carry_only_allowlisted_input(tmp_path, monkeypatch):
    git_dir, common = tmp_path / "wt-gitdir", tmp_path / "common"
    git_dir.mkdir()
    common.mkdir()
    dest = tmp_path / "dest"
    dest.mkdir()
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "filter.evil.smudge")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "evil")
    monkeypatch.setenv("GIT_OBJECT_DIRECTORY", str(tmp_path / "elsewhere"))
    monkeypatch.setenv("GIT_ALTERNATE_OBJECT_DIRECTORIES", str(tmp_path / "elsewhere"))
    seen: dict[str, Any] = {}

    def run_git(args, cwd, **kw):
        if args[0] == "rev-parse":
            return subprocess.CompletedProcess(
                args, 0, f"{git_dir.as_posix()}\n{common.as_posix()}\n", ""
            )
        if args[0] == "config":
            listing = "core.autocrlf\ntrue\0core.eol\nbogus\0"
            return subprocess.CompletedProcess(args, 0, listing, "")
        scratch = Path(kw["env"]["GIT_DIR"])
        seen.update(
            args=args,
            env=kw["env"],
            private=kw.get("private_dir"),
            config=(scratch / "config").read_text(encoding="utf-8"),
            alternates=(scratch / "objects" / "info" / "alternates").read_text(encoding="utf-8"),
        )
        return subprocess.CompletedProcess(args, 1, "", "fatal: boom")

    oid = "a" * 40
    got = worktree_populate.populate_worktree(run_git, str(dest), oid)

    assert got.returncode == 1 and got.stderr == "fatal: boom"
    env_seen = seen["env"]
    assert seen["args"] == ["read-tree", "-u", "--reset", oid]
    assert env_seen["GIT_DIR"] == seen["private"]
    assert env_seen["GIT_WORK_TREE"] == str(dest)
    assert env_seen["GIT_INDEX_FILE"] == os.path.join(git_dir.as_posix(), "index")
    assert env_seen["GIT_CONFIG_NOSYSTEM"] == "1" and env_seen["GIT_CONFIG_GLOBAL"] == os.devnull
    assert not any(k.startswith("GIT_CONFIG_KEY") or k == "GIT_CONFIG_COUNT" for k in env_seen)
    assert "GIT_OBJECT_DIRECTORY" not in env_seen
    assert "GIT_ALTERNATE_OBJECT_DIRECTORIES" not in env_seen
    assert seen["config"] == "[core]\n\trepositoryformatversion = 0\n\tautocrlf = true\n"
    assert seen["alternates"].strip() == os.path.join(common.as_posix(), "objects")
    assert not os.path.exists(seen["private"]), "the throwaway git dir was left behind"


@pytest.mark.parametrize("bad", ["", "HEAD", "-h", "a" * 39, "A" * 40, "g" * 40, "a" * 40 + "\n"])
def test_an_oid_that_is_not_an_object_id_is_refused_before_git_runs(bad):
    def run_git(*_a, **_kw):
        raise AssertionError("git ran for a bad oid")

    got = worktree_populate.populate_worktree(run_git, "/nowhere", bad)

    assert got.returncode != 0 and "not an object id" in got.stderr


def test_a_directory_name_with_a_line_separator_is_kept_whole(tmp_path):
    git_dir, common = tmp_path / "wt\u2028gitdir", tmp_path / "common\rdir"
    git_dir.mkdir()
    common.mkdir()
    seen: list[str] = []

    def run_git(args, cwd, **kw):
        if args[0] == "rev-parse":
            return subprocess.CompletedProcess(args, 0, f"{git_dir}\n{common}\n", "")
        if args[0] == "config":
            return subprocess.CompletedProcess(args, 1, "", "")
        seen.append(kw["env"]["GIT_INDEX_FILE"])
        return subprocess.CompletedProcess(args, 0, "", "")

    got = worktree_populate.populate_worktree(run_git, str(tmp_path), "a" * 40)

    assert got.returncode == 0, got.stderr
    assert seen == [os.path.join(str(git_dir), "index")]


def test_a_git_dir_git_does_not_name_absolutely_is_refused(tmp_path):
    def run_git(args, cwd, **_kw):
        return subprocess.CompletedProcess(args, 0, "relative\n.git\n", "")

    got = worktree_populate.populate_worktree(run_git, str(tmp_path), "a" * 40)

    assert got.returncode != 0 and "cannot locate" in got.stderr


@pytest.mark.parametrize(
    ("listing", "refused"),
    [
        ("extensions.partialclone\norigin\0", True),
        ("remote.origin.promisor\ntrue\0", True),
        ("remote.origin.promisor\0", True),
        ("remote.origin.promisor\n2\0", True),
        ("remote.origin.promisor\nfalse\0", False),
        ("remote.origin.promisor\n\0", False),
        ("core.autocrlf\ntrue\0", False),
    ],
)
def test_a_partial_clone_is_refused_before_anything_is_written(tmp_path, listing, refused):
    git_dir, common = tmp_path / "wt-gitdir", tmp_path / "common"
    git_dir.mkdir()
    common.mkdir()
    ran: list[str] = []

    def run_git(args, cwd, **_kw):
        ran.append(args[0])
        if args[0] == "rev-parse":
            return subprocess.CompletedProcess(
                args, 0, f"{git_dir.as_posix()}\n{common.as_posix()}\n", ""
            )
        if args[0] == "config":
            return subprocess.CompletedProcess(args, 0, listing, "")
        return subprocess.CompletedProcess(args, 0, "", "")

    got = worktree_populate.populate_worktree(run_git, str(tmp_path), "a" * 40)

    assert (got.returncode != 0) is refused
    assert ("read-tree" in ran) is not refused
    if refused:
        assert "partial clone" in got.stderr


def _scratch_leftovers() -> list[str]:
    root = agent_scratch.scratch_root()
    return [p.name for p in root.glob("kirocrew-populate-*")] if root.exists() else []


# --------------------------------------------------------------------------
# the callers: a filter planted AFTER the probe answered clean
# --------------------------------------------------------------------------
def _plant(env: dict[str, str], repo: Path, marker: Path) -> None:
    _ok(_git(env, repo, "config", "filter.evil.smudge", _driver(marker, "")))
    _ok(_git(env, repo, "config", "filter.evil.required", "true"))


def _unplant(env: dict[str, str], repo: Path) -> None:
    _git(env, repo, "config", "--remove-section", "filter.evil")


@pytest.fixture
def release_env(tmp_path, env, monkeypatch):
    """A clone whose newest release tag binds a filter in its own tree, with the release
    module's git layer replaced by direct runners. Returns ``(clone, marker, plant)`` where
    ``plant()`` writes the driver and the probe stand-in calls it AFTER answering clean."""
    origin, _ = _make_repo(
        tmp_path / "origin",
        env,
        {"payload.txt": "release payload\n"},
        attributes="payload.txt filter=evil\n",
    )
    _ok(_git(env, origin, "tag", "-a", "v0.10.0", "-m", "v0.10.0"))
    clone = tmp_path / "checkout"
    _ok(_git(env, tmp_path, "clone", "-q", str(origin), "checkout"))
    marker = tmp_path / "filter-ran"
    run_git = _run_git_for(env)

    async def run_cmd(cmd, **_kw):
        proc = _git(env, Path(cmd[2]), *cmd[3:])
        return proc.returncode, proc.stdout, proc.stderr

    async def remote() -> str:
        return "origin"

    def probe(_root: str) -> str:
        # The probe reads a clean config and answers "no filter"; the write lands after it,
        # in the gap before the add.
        _plant(env, clone, marker)
        return ""

    monkeypatch.setattr(repository, "_repo", lambda: str(clone))
    monkeypatch.setattr(repository, "_upstream_remote", remote)
    monkeypatch.setattr(runtime, "_run_cmd", run_cmd)
    monkeypatch.setattr(release_worktree, "_run_git", run_git)
    monkeypatch.setattr(release_worktree, "_checkout_filter", probe)
    monkeypatch.setattr(
        release_worktree.sandbox_tiers, "configured_sandbox_mode", lambda: "standard"
    )
    return clone, marker


@pytest.mark.asyncio
async def test_release_create_runs_no_filter_planted_after_the_probe(tmp_path, env, release_env):
    clone, marker = release_env
    peeled = _ok(_git(env, clone, "rev-list", "-n1", "v0.10.0"))

    # Control: with the driver planted, a plain ``worktree add`` of the release runs it.
    _plant(env, clone, marker)
    control = tmp_path / "control"
    _ok(_git(env, clone, "worktree", "add", "-q", "--detach", str(control), peeled))
    assert marker.exists(), "fixture never armed the driver: a plain checkout did not run it"
    marker.unlink()
    _ok(_git(env, clone, "worktree", "remove", "--force", str(control)))
    _unplant(env, clone)

    got = await release_worktree.create_release_worktree()

    assert got["ok"] is True, got
    assert not marker.exists(), "a filter planted after the probe ran during the checkout"
    dest = Path(got["path"])
    assert (dest / "payload.txt").read_bytes() == b"release payload\n"
    assert _ok(_git(env, dest, "rev-parse", "HEAD")) == peeled
    assert _git(env, dest, "symbolic-ref", "--quiet", "HEAD").returncode != 0
    assert _scratch_leftovers() == []


@pytest.mark.asyncio
async def test_release_create_removes_its_worktree_when_the_populate_fails(
    tmp_path, env, release_env, monkeypatch
):
    clone, _marker = release_env
    monkeypatch.setattr(
        release_worktree,
        "populate_worktree",
        lambda *_a: subprocess.CompletedProcess([], 128, "", "fatal: read-tree boom"),
    )

    got = await release_worktree.create_release_worktree()

    assert got["ok"] is False and got["code"] == "git_failed", got
    assert "read-tree boom" in got["error"]
    assert not Path(release_worktree.worktree_path(str(clone))).exists()
    assert release_worktree.WORKTREE_NAME not in _ok(_git(env, clone, "worktree", "list"))


@pytest.mark.asyncio
async def test_release_create_removes_its_worktree_when_no_sandbox_can_run_the_populate(
    tmp_path, env, release_env, monkeypatch
):
    clone, _marker = release_env

    def no_sandbox(*_a):
        raise release_worktree.SandboxUnavailable("no OS sandbox backend")

    monkeypatch.setattr(release_worktree, "populate_worktree", no_sandbox)

    got = await release_worktree.create_release_worktree()

    assert got["ok"] is False and got["code"] == "sandbox_unavailable", got
    assert not Path(release_worktree.worktree_path(str(clone))).exists()


@pytest.mark.asyncio
async def test_release_create_leaves_the_tree_after_a_populate_timeout_and_names_the_command(
    tmp_path, env, release_env, monkeypatch
):
    clone, _marker = release_env

    def slow(*_a):
        raise subprocess.TimeoutExpired(cmd="git read-tree", timeout=FALLBACK_TIMEOUT_SECS)

    monkeypatch.setattr(release_worktree, "populate_worktree", slow)

    got = await release_worktree.create_release_worktree()

    assert got["ok"] is False and got["code"] == "git_failed", got
    assert "git worktree remove -f -f" in got["error"]
    assert Path(release_worktree.worktree_path(str(clone))).exists()


@pytest.fixture
def dashboard_env(tmp_path, env, monkeypatch):
    """A repo and the dashboard's git layer replaced by direct runners. The probe stand-in
    answers clean and then plants the driver, as a write between the two spawns would."""
    repo, _oid = _make_repo(
        tmp_path / "repo", env, {"payload.txt": "payload\n"}, attributes="payload.txt filter=evil\n"
    )
    marker = tmp_path / "filter-ran"

    def probe(_root: str) -> str:
        _plant(env, repo, marker)
        return ""

    monkeypatch.setattr(dash_worktree, "_run_git", _run_git_for(env))
    monkeypatch.setattr(dash_worktree, "_checkout_filter", probe)
    return repo, marker


def test_the_follow_up_card_checkout_runs_no_filter_planted_after_the_probe(
    tmp_path, env, dashboard_env
):
    repo, marker = dashboard_env
    head = _ok(_git(env, repo, "rev-parse", "HEAD"))

    _plant(env, repo, marker)
    control = tmp_path / "control"
    _ok(_git(env, repo, "worktree", "add", "-q", "--detach", str(control), head))
    assert marker.exists(), "fixture never armed the driver: a plain checkout did not run it"
    marker.unlink()
    _ok(_git(env, repo, "worktree", "remove", "--force", str(control)))
    _unplant(env, repo)

    body, status = dash_worktree._create_worktree_sync(str(repo), "feat/x")

    assert status == 200, body
    assert not marker.exists(), "a filter planted after the probe ran during the checkout"
    dest = Path(body["path"])
    assert (dest / "payload.txt").read_bytes() == b"payload\n"
    assert _ok(_git(env, dest, "rev-parse", "--abbrev-ref", "HEAD")) == "feat/x"
    assert _ok(_git(env, dest, "ls-files")).split() == [".gitattributes", "payload.txt"]
    assert _scratch_leftovers() == []


def test_the_follow_up_card_unwinds_the_worktree_and_branch_when_the_populate_fails(
    tmp_path, env, dashboard_env, monkeypatch
):
    repo, _marker = dashboard_env
    monkeypatch.setattr(
        dash_worktree,
        "populate_worktree",
        lambda *_a: subprocess.CompletedProcess([], 128, "", "fatal: read-tree boom"),
    )

    body, status = dash_worktree._create_worktree_sync(str(repo), "feat/x")

    assert status == 400 and "read-tree boom" in body["error"], body
    assert not (tmp_path / "repo-wt-x").exists()
    assert _git(env, repo, "rev-parse", "--verify", "--quiet", "refs/heads/feat/x").returncode != 0


def test_the_follow_up_card_unwinds_when_the_populate_raises(
    tmp_path, env, dashboard_env, monkeypatch
):
    repo, _marker = dashboard_env

    def times_out(*_a):
        raise subprocess.TimeoutExpired(cmd="git read-tree", timeout=FALLBACK_TIMEOUT_SECS)

    monkeypatch.setattr(dash_worktree, "populate_worktree", times_out)

    with pytest.raises(subprocess.TimeoutExpired):
        dash_worktree._create_worktree_sync(str(repo), "feat/x")

    assert not (tmp_path / "repo-wt-x").exists()
    assert _git(env, repo, "rev-parse", "--verify", "--quiet", "refs/heads/feat/x").returncode != 0


def test_the_follow_up_card_refuses_a_partial_clone_and_leaves_nothing(tmp_path, env, monkeypatch):
    """A blobless clone's base commit may have no local blobs. A plain add fetched them; the
    populate cannot, so the card refuses up front and unwinds the worktree and branch."""
    origin, _ = _make_repo(tmp_path / "origin", env, {"payload.txt": "payload\n"})
    _ok(_git(env, origin, "config", "uploadpack.allowFilter", "true"))
    cloned = _git(env, tmp_path, "clone", "-q", "--filter=blob:none", origin.as_uri(), "repo")
    assert cloned.returncode == 0, cloned.stderr
    repo = tmp_path / "repo"
    assert _ok(_git(env, repo, "config", "--get", "remote.origin.promisor")) == "true"
    monkeypatch.setattr(dash_worktree, "_run_git", _run_git_for(env))
    monkeypatch.setattr(dash_worktree, "_checkout_filter", lambda _root: "")

    body, status = dash_worktree._create_worktree_sync(str(repo), "feat/x")

    assert status == 400 and "partial clone" in body["error"], body
    assert not (tmp_path / "repo-wt-x").exists()
    assert _git(env, repo, "rev-parse", "--verify", "--quiet", "refs/heads/feat/x").returncode != 0
    assert _scratch_leftovers() == []
