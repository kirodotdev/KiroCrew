"""A batch of anchors is resolved to ``os.path.realpath``'s answers, one folder read each.

The target-set rebuild asks the resolver child about a few hundred leaves under the
same home. ``_child_realpath.realpaths_sharing_folders`` reads each folder on the way
once per batch, where one ``realpath`` per leaf reads the home's folders again for
every leaf. These tests hold the batch to the standard library's answer for every
path, links and loops and odd spellings included, show a link still goes through
``os.path.realpath`` itself, and pin that nothing is kept from one batch to the next.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from conftest import requires_symlinks
from kiro_crew.security import _child_realpath, paths_lists, paths_resolver, paths_targets
from kiro_crew.security._child_realpath import realpaths_sharing_folders

posix_only = pytest.mark.skipif(os.sep != "/", reason="the shared walk reads '/'-separated paths")


def _one_by_one(paths: list[str]) -> list[str | None]:
    out: list[str | None] = []
    for path in paths:
        try:
            out.append(os.path.realpath(path))
        except (OSError, ValueError):
            out.append(None)
    return out


def _linked_tree(root: Path) -> Path:
    home = root / "home"
    (home / "real" / "aws").mkdir(parents=True)
    (home / ".config" / "gcloud").mkdir(parents=True)
    (root / "elsewhere" / "ssh").mkdir(parents=True)
    (home / "file").write_text("x", encoding="utf-8")
    os.symlink(home / "real" / "aws", home / ".aws")  # a linked leaf
    os.symlink("../elsewhere/ssh", home / ".ssh")  # a relative link through ``..``
    os.symlink(root / "nowhere", home / ".gnupg")  # a dangling link
    os.symlink(home / "loop_b", home / "loop_a")  # a loop of two links
    os.symlink(home / "loop_a", home / "loop_b")
    os.symlink(home / ".config", home / "cfg")  # a linked folder above leaves
    os.symlink(home / "cfg" / "gcloud", home / "chain")  # a link through a link
    os.symlink(home, root / "homelink")  # the home itself behind a link
    return home


_LEAVES = (
    "",
    ".aws",
    ".aws/credentials",
    ".ssh",
    ".ssh/id_rsa",
    ".gnupg",
    ".gnupg/x/y",
    "loop_a",
    "loop_a/x",
    "cfg/gcloud",
    "cfg/gcloud/a/b",
    "chain",
    "chain/a",
    ".config/gcloud",
    "missing",
    "missing/a/b",
    "file",
    "file/x",
    ".docker/config.json",
    "x/../.aws",
    "x/./y",
    "x//y",
    "trail/",
    ".aws/../.ssh/id_rsa",
)


@requires_symlinks
def test_every_answer_is_realpaths_answer(tmp_path):
    home = _linked_tree(tmp_path)
    paths = [
        os.path.join(base, leaf) if leaf else str(base)
        for base in (home, tmp_path / "homelink")
        for leaf in _LEAVES
    ]
    paths += ["/", "//", "//x/y", "relative/path", "", ".", "\0", str(home) + "\0"]
    assert realpaths_sharing_folders(paths) == _one_by_one(paths)
    # Order must not matter: a folder first met through a child is the same folder.
    assert realpaths_sharing_folders(paths[::-1]) == _one_by_one(paths[::-1])


@posix_only
def test_each_folder_is_read_once_per_batch(monkeypatch, tmp_path):
    home = tmp_path / "home"
    (home / "a" / "b").mkdir(parents=True)
    paths = [str(home / "a" / "b" / f"leaf-{i}") for i in range(20)]
    seen: list[str] = []
    real_lstat = os.lstat

    def counting(path, *args, **kwargs):  # type: ignore[no-untyped-def]
        seen.append(os.fspath(path))
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(_child_realpath.os, "lstat", counting)
    assert realpaths_sharing_folders(paths) == paths
    assert len(seen) == len(set(seen))
    assert set(paths) <= set(seen) and str(home / "a") in seen


@requires_symlinks
@posix_only
def test_a_link_is_resolved_by_the_standard_library(monkeypatch, tmp_path):
    home = _linked_tree(tmp_path)
    asked: list[str] = []
    real_realpath = os.path.realpath

    def recording(path, *args, **kwargs):  # type: ignore[no-untyped-def]
        asked.append(os.fspath(path))
        return real_realpath(path, *args, **kwargs)

    monkeypatch.setattr(_child_realpath.os.path, "realpath", recording)
    plain = str(home / ".config" / "gcloud")
    assert realpaths_sharing_folders([plain]) == [plain]
    assert asked == []
    linked = str(home / ".aws" / "credentials")
    assert realpaths_sharing_folders([linked]) == [str(home / "real" / "aws" / "credentials")]
    # The whole path, never the link alone: the standard library keeps its own
    # state for targets, chains and loops, and this walk composes none of it.
    assert asked == [linked]


@requires_symlinks
def test_a_link_whose_target_leads_back_through_itself_is_realpaths_answer(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    os.symlink(home, home / ".alias")
    os.symlink(home / ".alias" / ".config" / "..", home / ".config")
    os.symlink(home / ".config", home / "gcloud")
    paths = [
        str(home / leaf)
        for leaf in (
            ".config",
            ".config/gcloud",
            "gcloud",
            ".alias/.config/gcloud",
            ".config/gcloud/x",
            ".alias/.alias/.ssh",
        )
    ]
    assert realpaths_sharing_folders(paths) == _one_by_one(paths)
    assert realpaths_sharing_folders(paths[::-1]) == _one_by_one(paths[::-1])


@requires_symlinks
def test_nothing_is_kept_from_one_batch_to_the_next(tmp_path):
    home = tmp_path / "home"
    first = tmp_path / "first"
    second = tmp_path / "second"
    for folder in (home, first, second):
        folder.mkdir()
    os.symlink(first, home / ".aws")
    leaf = str(home / ".aws" / "credentials")
    assert realpaths_sharing_folders([leaf]) == [str(first / "credentials")]
    (home / ".aws").unlink()
    os.symlink(second, home / ".aws")
    assert realpaths_sharing_folders([leaf]) == [str(second / "credentials")]
    # A plain folder swapped for a link is read as the link by the next batch too.
    (home / ".config").mkdir()
    nested = str(home / ".config" / "gcloud")
    assert realpaths_sharing_folders([nested]) == [nested]
    (home / ".config").rmdir()
    os.symlink(second, home / ".config")
    assert realpaths_sharing_folders([nested]) == [str(second / "gcloud")]


def test_a_path_that_cannot_be_read_answers_none_and_the_rest_still_answer(monkeypatch, tmp_path):
    # Which spellings ``realpath`` refuses differs by platform (a NUL raises on POSIX
    # and resolves on Windows), so the refusal is injected for one relative path, which
    # every platform hands to ``os.path.realpath`` whole.
    real_realpath = os.path.realpath

    def refusing(path, *args, **kwargs):  # type: ignore[no-untyped-def]
        if os.fspath(path) == "unreadable":
            raise ValueError("embedded null byte")
        return real_realpath(path, *args, **kwargs)

    monkeypatch.setattr(_child_realpath.os.path, "realpath", refusing)
    paths = [str(tmp_path), "unreadable", str(tmp_path / "x")]
    assert realpaths_sharing_folders(paths) == [
        os.path.realpath(tmp_path),
        None,
        os.path.realpath(tmp_path / "x"),
    ]
    assert _child_realpath.realpath_many([os.fsencode(p) for p in paths]) == [
        os.fsencode(os.path.realpath(tmp_path)),
        b"",
        os.fsencode(os.path.realpath(tmp_path / "x")),
    ]


@requires_symlinks
def test_the_resolver_child_gives_the_same_set_as_this_interpreter(monkeypatch, tmp_path):
    """The rebuild through the pool's child and the inline build agree, links included."""
    home = _linked_tree(tmp_path)
    monkeypatch.setenv("KIROCREW_HOME", str(home / ".kiro" / "crew"))
    for name in ("KIRO_HOME", "KIROCREW_OS_HOME", *paths_targets._ADAPTER_ROOT_ENVS):
        monkeypatch.delenv(name, raising=False)
    paths_resolver._path_resolve_degraded.clear()
    roots = paths_targets._resolve_root_anchors(str(home))
    home_dirs = list(paths_lists._SENSITIVE_HOME_DIRS)
    inline = paths_targets._home_dir_targets_uncached(home_dirs, roots)
    pooled = paths_targets._rebuild_targets_bounded(home_dirs, roots)
    assert set(pooled) == set(inline)
    assert str(home / "real" / "aws").casefold() in pooled
