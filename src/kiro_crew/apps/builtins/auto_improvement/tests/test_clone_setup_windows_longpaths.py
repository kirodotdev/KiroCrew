"""Repository setup on Windows checks out a deep tracked path.

The setup clone runs with global and system git config disabled, so a user's
``core.longpaths=true`` never reaches it and Git for Windows applies its own
260-character limit to the checkout. On Windows the clone writes
``core.longpaths=true`` into the new repository's own config instead, which the
checkout and every later git call in that clone read. Nothing else about the
isolation changes: global and system config stay out, and the credential helper
stays the forge's own.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any
from unittest import mock

from kiro_crew.apps.builtins.auto_improvement.backend import clone_setup
from kiro_crew.apps.builtins.auto_improvement.backend.clone_setup import CloneSpec
from kiro_crew.platform_compat import IS_WINDOWS
from kiro_crew.testing.wait import default_timeout

_SPEC = CloneSpec("o/r", "https://github.com/o/r.git", "o--r", "github", "github.com", "o/r")


def _clone_call(tmp_path: Path, *, windows: bool) -> tuple[list[str], dict[str, str]]:
    """The argv and env ``setup_safe_clone`` hands to ``git clone``."""
    root = tmp_path / "root"
    root.mkdir()
    calls: list[tuple[list[str], dict[str, str]]] = []

    def _run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((list(argv), dict(kwargs["env"])))
        return subprocess.CompletedProcess(argv, 128, "", "fatal: stop here\n")

    with (
        mock.patch.object(clone_setup, "IS_WINDOWS", windows),
        mock.patch.object(clone_setup, "validate_target_url", return_value=(_SPEC, "")),
        mock.patch.object(clone_setup.subprocess, "run", side_effect=_run),
    ):
        _, err = clone_setup.setup_safe_clone("https://github.com/o/r", root)

    assert err == "git clone failed: fatal: stop here"
    clones = [call for call in calls if "clone" in call[0]]
    assert len(clones) == 1
    return clones[0]


def test_a_windows_clone_writes_longpaths_into_the_new_repository(tmp_path: Path) -> None:
    argv, env = _clone_call(tmp_path, windows=True)

    clone_at = argv.index("clone")
    assert argv[clone_at + 1 : clone_at + 3] == ["--config", "core.longpaths=true"]
    assert argv.index(_SPEC.clone_url) > clone_at + 2
    # The isolation is unchanged: no user or system config, the forge's helper only.
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull
    assert env["GIT_CONFIG_SYSTEM"] == os.devnull
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert [a for a in argv if a.startswith("credential.helper=")] == [
        "credential.helper=!gh auth git-credential"
    ]


def test_a_posix_clone_argv_carries_no_longpaths(tmp_path: Path) -> None:
    argv, env = _clone_call(tmp_path, windows=False)

    assert not any("longpaths" in a for a in argv)
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull
    assert env["GIT_CONFIG_SYSTEM"] == os.devnull
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"


def _git(*args: str, cwd: Path, env: dict[str, str] | None = None) -> str:
    return subprocess.run(
        ["git", *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=cwd,
        env=env,
        timeout=default_timeout(),
    ).stdout.strip()


#: 305 characters inside the repository, so the checkout path passes 260 under
#: any temp root, however short.
_DEEP_PATH = "/".join(["level-of-a-deeply-nested-tree"] * 10) + "/f.txt"


def _bare_with_deep_path(tmp_path: Path) -> Path:
    """A bare repository whose one commit tracks ``_DEEP_PATH``, built with plumbing
    so the fixture itself never writes the deep path to disk."""
    bare = tmp_path / "remote.git"
    _git("init", "-q", "--bare", str(bare), cwd=tmp_path)
    git_dir = ("--git-dir", str(bare))
    index_env = {**os.environ, "GIT_INDEX_FILE": str(tmp_path / "seed.index")}
    blob = subprocess.run(
        ["git", *git_dir, "hash-object", "-w", "--stdin"],
        input="deep\n",
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=tmp_path,
        timeout=default_timeout(),
    ).stdout.strip()
    _git(
        *git_dir,
        "update-index",
        "--add",
        "--cacheinfo",
        f"100644,{blob},{_DEEP_PATH}",
        cwd=tmp_path,
        env=index_env,
    )
    tree = _git(*git_dir, "write-tree", cwd=tmp_path, env=index_env)
    commit = _git(
        *git_dir,
        "-c",
        "user.name=t",
        "-c",
        "user.email=a@b.c",
        "commit-tree",
        tree,
        "-m",
        "seed",
        cwd=tmp_path,
    )
    _git(*git_dir, "update-ref", "refs/heads/main", commit, cwd=tmp_path)
    _git(*git_dir, "symbolic-ref", "HEAD", "refs/heads/main", cwd=tmp_path)
    return bare


def test_a_setup_checks_out_a_path_past_the_260_character_limit(tmp_path: Path) -> None:
    """Git for Windows refuses the path without ``core.longpaths``; elsewhere the same
    real clone guards that the deep checkout still works and the key stays unset."""
    bare = _bare_with_deep_path(tmp_path)
    root = tmp_path / "root"
    spec = CloneSpec("o/r", bare.as_uri(), "o--r", "github", "github.com", "o/r")
    deep_file = root / "o--r" / _DEEP_PATH
    assert len(str(deep_file)) > 260

    with mock.patch.object(clone_setup, "validate_target_url", return_value=(spec, "")):
        result, err = clone_setup.setup_safe_clone(
            "https://github.com/o/r",
            root,
            timeout_s=int(default_timeout()),  # flake-ok: the clone takes whole seconds
        )

    assert err == ""
    assert result["push_disabled"] is True
    clone = root / "o--r"
    local = _git("config", "--local", "--list", cwd=clone).splitlines()
    if IS_WINDOWS:
        assert "core.longpaths=true" in local
        # ``\\?\`` lets the probe stat the long path whatever the host's registry says.
        assert os.path.isfile("\\\\?\\" + os.path.abspath(deep_file))
    else:
        assert not [line for line in local if line.startswith("core.longpaths=")]
        assert deep_file.is_file()
    assert _git("status", "--porcelain", cwd=clone) == ""
