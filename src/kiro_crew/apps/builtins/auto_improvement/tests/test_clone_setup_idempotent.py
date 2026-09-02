"""Offline regressions for idempotent push-disabled clone setup."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from kiro_crew import platform_compat
from kiro_crew.apps.builtins.auto_improvement.backend import clone_setup
from kiro_crew.apps.builtins.auto_improvement.backend.clone_setup import (
    DISABLED_NO_PUSH,
    CloneSpec,
)


def _seeded_bare(tmp_path: Path) -> Path:
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True, cwd=tmp_path)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", "-q", str(bare), str(seed)], check=True, cwd=tmp_path)
    (seed / "f.txt").write_text("hi")
    subprocess.run(["git", "-C", str(seed), "add", "f.txt"], check=True, cwd=tmp_path)
    subprocess.run(
        [
            "git",
            "-C",
            str(seed),
            "-c",
            "user.email=a@b.c",
            "-c",
            "user.name=t",
            "commit",
            "-qm",
            "seed",
        ],
        check=True,
        cwd=tmp_path,
    )
    subprocess.run(
        ["git", "-C", str(seed), "push", "-q", "origin", "HEAD:main"],
        check=True,
        cwd=tmp_path,
    )
    subprocess.run(
        ["git", "--git-dir", str(bare), "symbolic-ref", "HEAD", "refs/heads/main"],
        check=True,
        cwd=tmp_path,
    )
    return bare


def _spec(bare: Path) -> CloneSpec:
    return CloneSpec("o/r", bare.as_uri(), "o--r")


def _setup(bare: Path, root: Path) -> tuple[dict, str]:
    with mock.patch.object(clone_setup, "validate_target_url", return_value=(_spec(bare), "")):
        return clone_setup.setup_safe_clone("https://github.com/o/r", root)


def test_second_setup_reuses_the_neutralized_clone(tmp_path: Path) -> None:
    bare = _seeded_bare(tmp_path)
    root = tmp_path / "root"

    first, first_err = _setup(bare, root)
    second, second_err = _setup(bare, root)

    assert first_err == "" and first["reused"] is False
    assert second_err == "" and second["reused"] is True
    assert second["push_disabled"] is True
    clone = root / "o--r"
    assert clone_setup._origin_urls(clone, push=False) == [DISABLED_NO_PUSH]
    assert clone_setup._origin_urls(clone, push=True) == [DISABLED_NO_PUSH]


def test_clone_start_failure_returns_controlled_error_without_cleanup(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    spec = CloneSpec("o/r", (tmp_path / "remote.git").as_uri(), "o--r")
    with (
        mock.patch.object(clone_setup, "validate_target_url", return_value=(spec, "")),
        mock.patch.object(clone_setup.subprocess, "run", side_effect=OSError("git missing")),
        mock.patch.object(clone_setup, "rmtree_force") as cleanup,
    ):
        result, err = clone_setup.setup_safe_clone("https://github.com/o/r", root)

    assert result == {}
    assert err == "git clone could not start: git missing"
    cleanup.assert_not_called()
    assert not (root / "o--r").exists()


def test_failed_clone_with_no_destination_returns_controlled_error(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    spec = CloneSpec("o/r", (tmp_path / "remote.git").as_uri(), "o--r")
    failed = subprocess.CompletedProcess(["git", "clone"], 128, "", "clone failed")
    with (
        mock.patch.object(clone_setup, "validate_target_url", return_value=(spec, "")),
        mock.patch.object(clone_setup.subprocess, "run", return_value=failed),
    ):
        result, err = clone_setup.setup_safe_clone("https://github.com/o/r", root)

    assert result == {}
    assert err == "git clone failed: clone failed"
    assert not (root / "o--r").exists()


def test_extra_origin_value_refuses_reuse(tmp_path: Path) -> None:
    bare = _seeded_bare(tmp_path)
    root = tmp_path / "root"
    first, err = _setup(bare, root)
    assert err == "" and first["push_disabled"] is True
    clone = root / "o--r"
    subprocess.run(
        [
            "git",
            "-C",
            str(clone),
            "config",
            "--add",
            "remote.origin.url",
            bare.as_uri(),
        ],
        check=True,
        cwd=tmp_path,
    )

    branches, branch_err = clone_setup.list_clone_branches(clone)
    checked_out, checkout_err = clone_setup.checkout_branch(clone, "main")
    result, reuse_err = _setup(bare, root)

    assert branches == []
    assert "not push-disabled" in branch_err
    assert checked_out is False
    assert "not push-disabled" in checkout_err
    assert result == {}
    assert "ambiguous origin URLs" in reuse_err


def test_disable_push_replaces_every_url_value(tmp_path: Path) -> None:
    bare = _seeded_bare(tmp_path)
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(bare), str(clone)], check=True, cwd=tmp_path)
    for key in ("remote.origin.url", "remote.origin.pushurl"):
        subprocess.run(
            ["git", "-C", str(clone), "config", "--add", key, bare.as_uri()],
            check=True,
            cwd=tmp_path,
        )

    clone_setup._disable_push(clone)

    assert clone_setup._origin_urls(clone, push=False) == [DISABLED_NO_PUSH]
    assert clone_setup._origin_urls(clone, push=True) == [DISABLED_NO_PUSH]


def test_retire_unsafe_clone_preserves_bytes_off_canonical_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clone = tmp_path / "clone"
    clone.mkdir()
    (clone / "evidence.txt").write_text("preserve", encoding="utf-8")
    # Some CI/dev temp roots live below a system symlink (for example /var on
    # macOS). Setup tests cover ancestor refusal separately; this regression
    # isolates the anchored no-replace rename itself.
    monkeypatch.setattr(clone_setup, "first_linked_ancestor", lambda _path: None)
    monkeypatch.setattr(clone_setup, "is_link_or_junction", lambda _path: False)

    retired = clone_setup._retire_unsafe_clone(clone)

    assert retired is not None

    assert not clone.exists()
    assert retired.parent.parent == tmp_path
    assert retired.parent.name.startswith(".clone.unsafe-")
    assert retired.name == "clone"
    assert (retired / "evidence.txt").read_text(encoding="utf-8") == "preserve"

    latest = retired
    for index in range(4):
        clone.mkdir()
        (clone / "evidence.txt").write_text(str(index), encoding="utf-8")
        next_retired = clone_setup._retire_unsafe_clone(clone)
        assert next_retired is not None
        latest = next_retired

    retained = sorted(tmp_path.glob(".clone.unsafe-*"))
    assert len(retained) == clone_setup._UNSAFE_CLONE_RETENTION
    assert latest.exists()


def test_linked_scratch_root_is_refused_before_mutation(tmp_path: Path) -> None:
    bare = _seeded_bare(tmp_path)
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    root = tmp_path / "root"
    try:
        platform_compat.symlink_or_junction(foreign, root)
    except OSError as exc:  # pragma: no cover - host policy may forbid links
        pytest.skip(f"cannot create a directory link: {exc}")

    result, err = _setup(bare, root)

    assert result == {}
    assert "link or junction" in err
    assert not list(foreign.iterdir())


@pytest.mark.parametrize(
    "directive",
    [
        "include",
        "url",
        "worktree",
        "diff_external",
        "askpass",
        "attributes_file",
        "excludes_file",
    ],
)
def test_unsafe_git_config_refuses_reuse(tmp_path: Path, directive: str) -> None:
    bare = _seeded_bare(tmp_path)
    root = tmp_path / "root"
    first, err = _setup(bare, root)
    assert err == "" and first["push_disabled"] is True
    config = root / "o--r" / ".git" / "config"
    additions = {
        "include": "\n[include]\n\tpath = //attacker/share/config\n",
        "url": '\n[url "https://attacker.invalid/"]\n\tinsteadOf = DISABLED_NO_PUSH\n',
        "worktree": f"\n[core]\n\tworktree = {tmp_path / 'foreign'}\n",
        "diff_external": "\n[diff]\n\texternal = /attacker/host-code\n",
        "askpass": "\n[core]\n\taskpass = /attacker/credential-helper\n",
        "attributes_file": "\n[core]\n\tattributesFile = //attacker/share/attributes\n",
        "excludes_file": "\n[core]\n\texcludesFile = //attacker/share/excludes\n",
    }
    with config.open("a", encoding="utf-8") as handle:
        handle.write(additions[directive])

    result, reuse_err = _setup(bare, root)

    assert result == {}
    assert "metadata safety verification" in reuse_err


def test_fifo_git_metadata_is_refused_without_opening_it(tmp_path: Path) -> None:
    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFOs are unavailable on this platform")
    bare = _seeded_bare(tmp_path)
    root = tmp_path / "root"
    first, err = _setup(bare, root)
    assert err == "" and first["push_disabled"] is True
    clone = root / "o--r"
    index = clone / ".git" / "index"
    index.unlink()
    os.mkfifo(index)

    assert clone_setup._repository_is_safe(clone) is False
    result, reuse_err = _setup(bare, root)
    assert result == {}
    assert "metadata safety verification" in reuse_err


def test_hardlinked_git_metadata_is_refused_without_external_write(tmp_path: Path) -> None:
    bare = _seeded_bare(tmp_path)
    root = tmp_path / "root"
    first, err = _setup(bare, root)
    assert err == "" and first["push_disabled"] is True
    clone = root / "o--r"
    external = tmp_path / "external"
    external.write_text("keep me")
    attributes = clone / ".git" / "info" / "attributes"
    attributes.parent.mkdir(parents=True, exist_ok=True)
    attributes.unlink(missing_ok=True)
    try:
        os.link(external, attributes)
    except OSError as exc:  # pragma: no cover - filesystem may forbid hardlinks
        pytest.skip(f"cannot create metadata hardlink: {exc}")

    branches, branch_err = clone_setup.list_clone_branches(clone)
    checked_out, checkout_err = clone_setup.checkout_branch(clone, "main")
    result, reuse_err = _setup(bare, root)

    assert branches == []
    assert "safety verification" in branch_err
    assert checked_out is False
    assert "safety verification" in checkout_err
    assert result == {}
    assert "metadata safety verification" in reuse_err
    assert external.read_text() == "keep me"


def test_linked_git_directory_is_refused_before_target_access(tmp_path: Path) -> None:
    bare = _seeded_bare(tmp_path)
    root = tmp_path / "root"
    first, err = _setup(bare, root)
    assert err == "" and first["push_disabled"] is True
    clone = root / "o--r"
    real_git = clone / ".git-real"
    (clone / ".git").rename(real_git)
    foreign = tmp_path / "foreign-git"
    foreign.mkdir()
    try:
        platform_compat.symlink_or_junction(foreign, clone / ".git")
    except OSError as exc:  # pragma: no cover - host policy may forbid links
        pytest.skip(f"cannot create Git directory link: {exc}")

    result, reuse_err = _setup(bare, root)

    assert result == {}
    assert "linked Git directory" in reuse_err
    assert not list(foreign.iterdir())
