"""Offline tests pinning idempotent, push-disabled clone setup."""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from kiro_crew import platform_compat, sandbox, security
from kiro_crew.apps.builtins.auto_improvement.backend import clone_setup
from kiro_crew.apps.builtins.auto_improvement.backend.clone_setup import (
    DISABLED_NO_PUSH,
    CloneSpec,
)
from kiro_crew.platform_compat import rmtree_force


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


@pytest.fixture
def marker_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the quarantine marker's crew-home root at a temp dir.

    ``_quarantine_root`` resolves it through ``config.paths.data_home()``, the real crew data
    home -- writing markers there from a test would leak state between runs and could refuse
    a developer's own clone. Patched on the name ``clone_setup`` imported, so the redirect
    holds however the caller reaches it.
    """
    home = tmp_path / "crewhome"
    home.mkdir()
    monkeypatch.setattr(clone_setup, "data_home", lambda: home)
    return home / clone_setup._QUARANTINE_DIR_LEAF


def test_a_quarantined_clone_is_refused_for_reuse(tmp_path: Path, marker_root: Path) -> None:
    """A clone whose rollback AND retirement both failed must not be reused.

    Reuse attests git metadata, config and origin URLs -- none of which look at what the
    branch tip points AT -- so a clone still carrying a REFUSED, unscanned provisional commit
    passes every one of those checks. The next run would commit its winner on top and publish
    the refused commit as an ancestor. The marker is what makes the refusal outlive the
    process that discovered it, since the un-rolled-back commit is on disk.
    """
    bare = _seeded_bare(tmp_path)
    root = tmp_path / "root"
    result, err = _setup(bare, root)
    assert result and not err
    clone = Path(result["clone"])

    marker = clone_setup._mark_clone_quarantined(clone, "rollback to abc0123456 failed")
    assert marker is not None

    again, err = _setup(bare, root)
    assert again == {}
    assert "quarantined" in err


def test_the_marker_lives_outside_the_tree_the_agent_works_in(
    tmp_path: Path, marker_root: Path
) -> None:
    """The marker must not sit in the scratch directory, which the agent writes to.

    It lives at a TOP-LEVEL crew-home leaf that is bind-masked from every agent sandbox and
    fenced from agent file tools. Top-level matters on its own: a mask covers the name it is
    bound over and not that name's ancestors, so a leaf under an agent-writable directory
    could be renamed out from under the mount.
    """
    bare = _seeded_bare(tmp_path)
    root = tmp_path / "root"
    result, _ = _setup(bare, root)
    clone = Path(result["clone"])

    marker = clone_setup._quarantine_marker(clone)
    assert marker is not None
    assert marker.parent == marker_root, "the marker is not under the masked crew-home leaf"
    assert not marker.is_relative_to(root), "the marker is inside the agent's scratch root"
    assert not marker.is_relative_to(clone)
    # The name is a hash of the clone path, so a repository name cannot steer it.
    assert marker.name.endswith(".json") and len(marker.stem) == 64
    assert "/" not in marker.stem and "\\" not in marker.stem
    # Both gates that fence the leaf name it, so neither can drift from the other.
    # ``sensitive_home_dirs`` reports crew-home-PREFIXED paths, one per home spelling.
    assert clone_setup._QUARANTINE_DIR_LEAF in sandbox._CREW_HIDDEN_LEAVES
    fenced = [
        d for d in security.sensitive_home_dirs() if d.endswith(clone_setup._QUARANTINE_DIR_LEAF)
    ]
    assert fenced, "the quarantine leaf is masked from sandboxes but not fenced from file tools"


def test_a_dangling_marker_symlink_reads_as_quarantined(tmp_path: Path, marker_root: Path) -> None:
    """A planted entry can only make the guard MORE conservative, never less.

    The marking open refuses to follow links, so a dangling symlink at the marker name is an
    existing entry to it -- while `exists()` would call the same entry absent. Split that way,
    a planted dangling link would let marking report success while the guard read no marker,
    and the poisoned clone would be reused. `lstat` closes it: anything at the name counts.
    """
    clone = tmp_path / "clone"
    clone.mkdir()
    marker = clone_setup._quarantine_marker(clone)
    assert marker is not None
    try:
        marker.symlink_to(tmp_path / "does-not-exist")
    except OSError as exc:  # pragma: no cover - host policy may forbid links
        pytest.skip(f"cannot create a symlink: {exc}")

    assert marker.exists() is False, "precondition: a dangling link looks absent to exists()"
    assert clone_setup._clone_is_quarantined(clone) is True
    # And marking over it reports the guard as standing rather than claiming a fresh write.
    assert clone_setup._mark_clone_quarantined(clone, "planted") == marker


def test_a_linked_marker_root_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A link planted at the ROOT would put every marker outside the fence, and no per-marker
    check can see that -- so the root is refused and the guard fails closed."""
    home = tmp_path / "crewhome"
    home.mkdir()
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    try:
        platform_compat.symlink_or_junction(foreign, home / clone_setup._QUARANTINE_DIR_LEAF)
    except OSError as exc:  # pragma: no cover - host policy may forbid links
        pytest.skip(f"cannot create a directory link: {exc}")
    monkeypatch.setattr(clone_setup, "data_home", lambda: home)

    assert clone_setup._quarantine_root() is None
    assert clone_setup._mark_clone_quarantined(tmp_path, "unusable root") is None
    # Fails CLOSED: a guard that cannot be consulted must not certify the clone.
    assert clone_setup._clone_is_quarantined(tmp_path) is True


def test_the_marker_write_cannot_truncate_an_existing_file(
    tmp_path: Path, marker_root: Path
) -> None:
    """`O_TRUNC` is absent from the open, so an existing marker is never shortened.

    An existing marker is the guard already standing, not a failure -- presence is the whole
    signal and the contents are diagnostic -- so marking twice reports the same path and
    leaves the first write intact.
    """
    clone = tmp_path / "clone"
    clone.mkdir()
    first = clone_setup._mark_clone_quarantined(clone, "first failure")
    assert first is not None
    before = first.read_bytes()

    second = clone_setup._mark_clone_quarantined(clone, "second failure")

    assert second == first
    assert first.read_bytes() == before, "the second mark truncated or rewrote the first"
    assert clone_setup._clone_is_quarantined(clone) is True


def test_the_quarantine_marker_clears_when_the_clone_is_gone(
    tmp_path: Path, marker_root: Path
) -> None:
    """The guard is scoped to the DIRECTORY it names, which is how it answers "when does it
    clear": a later successful retirement, or an operator removing the tree, removes the thing
    the marker names, so nothing is left permanently refusing a name whose bytes are gone. The
    stale marker is pruned on the way past rather than accumulating one file per clone."""
    bare = _seeded_bare(tmp_path)
    root = tmp_path / "root"
    result, _ = _setup(bare, root)
    clone = Path(result["clone"])
    marker = clone_setup._mark_clone_quarantined(clone, "retirement failed")
    assert marker is not None and clone_setup._clone_is_quarantined(clone) is True

    rmtree_force(clone)

    assert clone_setup._clone_is_quarantined(clone) is False
    assert not marker.exists(), "the stale marker was not pruned"
    fresh, err = _setup(bare, root)
    assert fresh and not err, err


def test_the_marker_survives_a_clone_that_cannot_be_written_into(
    tmp_path: Path, marker_root: Path
) -> None:
    """Retirement fails on Windows because a handle is held INSIDE the tree, and that same
    cause would block a marker written under it -- which is one reason the marker is not a
    child of the clone. Here the clone directory is made unwritable to stand in for that."""
    clone = tmp_path / "clone"
    clone.mkdir()
    original = clone.stat().st_mode
    os.chmod(clone, 0o500)
    try:
        marker = clone_setup._mark_clone_quarantined(clone, "retirement failed")
        assert marker is not None, "a read-only clone must not defeat the marker"
        assert marker.exists()
        assert clone_setup._clone_is_quarantined(clone) is True
    finally:
        os.chmod(clone, original)


def test_an_unwritable_marker_root_refuses_every_clone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A root that reads but cannot be written is the one state that fails OPEN otherwise.

    The marking open fails, so nothing is recorded; the reuse check then finds no marker and
    certifies the clone, with both halves reporting nothing wrong. Proving writability turns
    that into a refusal, because the root that could not take the write fails the probe.
    """
    home = tmp_path / "crewhome"
    home.mkdir()
    root = home / clone_setup._QUARANTINE_DIR_LEAF
    root.mkdir()
    monkeypatch.setattr(clone_setup, "data_home", lambda: home)
    original = root.stat().st_mode
    os.chmod(root, 0o500)
    try:
        if os.access(root, os.W_OK):  # pragma: no cover - root or a permissive filesystem
            pytest.skip("cannot make a directory unwritable for this user")
        assert clone_setup._quarantine_root() is None
        assert clone_setup._mark_clone_quarantined(tmp_path, "unwritable root") is None
        # The clone is NOT certified: a guard that cannot record cannot clear anything.
        assert clone_setup._clone_is_quarantined(tmp_path) is True
    finally:
        os.chmod(root, original)


def test_a_clone_is_removed_when_neither_marker_nor_retirement_works(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The canonical name must not survive all three levers.

    Recording a marker and renaming the tree aside fail from independent causes, so either
    alone suffices. When both fail the clone still sits at the name the next run looks up, so
    the tree is removed outright -- a destination that exists without a `.git` directory is
    refused by `_setup_safe_clone`.
    """
    from kiro_crew.apps.builtins.auto_improvement.spine import driver as D

    clone = tmp_path / "clone"
    (clone / ".git").mkdir(parents=True)
    monkeypatch.setattr(clone_setup, "_mark_clone_quarantined", lambda *_a, **_k: None)
    monkeypatch.setattr(clone_setup, "_retire_unsafe_clone", lambda _clone: None)
    # A real Driver without ``__init__``, the convention the dogfood suite uses: a
    # SimpleNamespace is not a Driver, so mypy rejects it as the self argument.
    d = object.__new__(D.Driver)
    d.clone = clone
    d.log = logging.getLogger("t")
    d._repository_retired = False
    monkeypatch.setattr(D.Driver, "_progress", lambda _self, **_k: None)

    D.Driver._quarantine_unrolled_clone(d, "abc0123456")

    assert not clone.exists(), "the clone survived every guard and is still reusable"


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
