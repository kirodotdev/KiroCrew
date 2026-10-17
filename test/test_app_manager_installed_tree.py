"""The installed-tree preview the install-time desktop gate judges:
``copy_app_tree_as_installed``, ``gateway_data_dir_obstruction`` and
``preserved_data_awaits``. ``test_app_manager.py`` heads the suite and lists its parts.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest
from app_manager_helpers import _DATA_IS_A_FILE, _DATA_IS_A_LINK

from conftest import requires_symlinks


def _folds_by_a_direct_look(directory: Path) -> bool:
    """Whether names in *directory* fold case, asked of the filesystem directly (a
    file created and looked up under its upper-cased name), so a probe's verdict
    can be checked against the host's truth instead of a platform guess."""
    marker = directory / "case-look"
    marker.touch()
    try:
        return (directory / "CASE-LOOK").exists()
    finally:
        marker.unlink()


def _data_dir_mkdir_accepts(data: Path) -> bool:
    """What `app_data_dir()` does at that name, as a verdict: `mkdir(exist_ok=True)`
    stands beside a directory (a link resolving to one included) and fails on
    anything else -- on Windows that includes a file-type link at a directory,
    which `is_dir` cannot follow. The predicate under test must answer the same on
    every host, so the tests derive the expectation from this call rather than
    from the platform."""
    try:
        data.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    return True


class TestCopyAppTreeAsInstalled:
    """The tree the install-time desktop gate judges: the install's own copy plus
    the gateway's post-copy part, produced -- never predicted. Only the two tests
    that create a link need the symlink privilege; the rest run on every host,
    NTFS included, where the install's direct paths reach a case-variant entry
    exactly as they do in the app directory."""

    def _source(self, tmp_path: Path) -> Path:
        # Under a directory named like the real sources dir, so a link text can
        # re-enter the tree from above by naming it.
        root = tmp_path / "app-sources" / "demo"
        (root / "requirements").mkdir(parents=True)
        (root / "requirements" / "prod.txt").write_text("fastapi\n", encoding="utf-8")
        (root / "server.py").write_text("", encoding="utf-8")
        return root

    def test_the_gateway_owned_root_entries_are_removed_by_the_install_s_own_paths(self, tmp_path):
        """`install_app` and `update_app` remove `.app_secret` and (with a preserved
        directory to put back) `data` by direct path; the preview makes the very same
        calls, so the filesystem decides a case variant for both alike: where names
        fold (APFS, NTFS) `dest / "data"` IS the shipped `Data/` and it goes; where
        they do not (ext4) `Data/` is the app's own directory and stays. Checked
        against a direct look at this host, not a platform guess."""
        from kiro_crew.apps.manager import copy_app_tree_as_installed

        root = self._source(tmp_path)
        (root / "Data").mkdir()
        (root / "Data" / "server.py").write_text("", encoding="utf-8")
        (root / ".app_secret").write_text("secret\n", encoding="utf-8")
        (root / "backend").mkdir()
        (root / "backend" / "data").mkdir()
        (root / "backend" / "data" / "kept.txt").write_text("", encoding="utf-8")
        # The exact name ships from a SECOND source: `Data/` and `data/` cannot
        # coexist in one directory on a folding filesystem, and this test runs on
        # NTFS and APFS too.
        exact = tmp_path / "app-sources-exact" / "demo"
        (exact / "data").mkdir(parents=True)
        (exact / "data" / "seed.txt").write_text("", encoding="utf-8")
        (exact / "server.py").write_text("", encoding="utf-8")
        (exact / ".app_secret").write_text("secret\n", encoding="utf-8")

        folds = _folds_by_a_direct_look(tmp_path)
        dest = tmp_path / "installed"
        copy_app_tree_as_installed(root, dest, data_preserved=True)
        assert not os.path.lexists(dest / "data")  # the install's own path, gone everywhere
        # On a folding filesystem that path WAS `Data/`; elsewhere `Data/` is the
        # app's own directory, carried as itself -- the install's answer too.
        assert (dest / "Data" / "server.py").is_file() is (not folds)
        assert not os.path.lexists(dest / ".app_secret")
        assert (dest / "backend" / "data" / "kept.txt").is_file()  # nested: the app's own
        assert (dest / "server.py").is_file()

        dest = tmp_path / "installed-exact"
        copy_app_tree_as_installed(exact, dest, data_preserved=True)
        assert not os.path.lexists(dest / "data")
        assert not os.path.lexists(dest / ".app_secret")
        assert (dest / "server.py").is_file()

    def test_a_first_install_carries_the_source_s_data_dir_as_itself(self, tmp_path):
        """With no preserved `data/` to put back -- a first install -- `install_app`
        copies the source's `data/` and leaves it: an entry point under it is the
        source's there, so the preview keeps it. `.app_secret` is the gateway's on
        every install (written after the copy) and goes regardless."""
        from kiro_crew.apps.manager import copy_app_tree_as_installed

        root = self._source(tmp_path)
        (root / "data").mkdir()
        (root / "data" / "server.py").write_text("", encoding="utf-8")
        (root / ".app_secret").write_text("secret\n", encoding="utf-8")
        dest = tmp_path / "installed-fresh"
        copy_app_tree_as_installed(root, dest, data_preserved=False)
        assert (dest / "data" / "server.py").is_file()
        assert not os.path.lexists(dest / ".app_secret")

    def test_a_root_data_entry_the_data_dir_cannot_stand_beside_is_refused_on_a_first_install(
        self, tmp_path
    ):
        """The gate judged only the layout it was about and waived an app shipping a
        root FILE named `data`; `install_app` then wrote its record and raised at
        `app_data_dir()`. The preview asks the install's own question
        (`gateway_data_dir_obstruction`) and raises the install's own refusal, so
        the gate turns the tree away before any transaction touches the app
        directory. With a preserved `data/` awaiting, the copied entry is removed
        (the install puts the directory back) and the same source passes."""
        from kiro_crew.apps.manager import (
            InstalledTreeRefused,
            copy_app_tree_as_installed,
            gateway_data_dir_obstruction,
        )

        root = self._source(tmp_path)
        (root / "data").write_text("a file\n", encoding="utf-8")
        dest = tmp_path / "installed-fresh"
        with pytest.raises(InstalledTreeRefused) as refused:
            copy_app_tree_as_installed(root, dest, data_preserved=False)
        assert str(refused.value) == _DATA_IS_A_FILE
        assert (
            gateway_data_dir_obstruction(dest) == _DATA_IS_A_FILE
        )  # the tree it refused, as copied
        dest = tmp_path / "installed-update"
        copy_app_tree_as_installed(root, dest, data_preserved=True)
        assert not os.path.lexists(dest / "data")
        assert gateway_data_dir_obstruction(dest) == ""
        # The predicate answers exactly as `mkdir(exist_ok=True)` would: a directory
        # (or nothing) is fine; anything else at the name is not.
        assert gateway_data_dir_obstruction(tmp_path / "does-not-exist") == ""
        (dest / "data").mkdir()
        assert gateway_data_dir_obstruction(dest) == ""

    def test_an_entry_that_cannot_be_followed_to_a_directory_is_refused_not_raised(
        self, tmp_path, monkeypatch
    ):
        """On Windows a file-type link at a directory cannot be stat'ed through:
        `is_dir` raises `PermissionError` -- and so would the data directory's own
        `mkdir(exist_ok=True)`, after the record. The predicate must answer that
        shape as a refusal, never let the raise out (which the install's copy branch
        would report as a failed copy). Pinned on every host by making the follow
        fail the way that platform does."""
        from kiro_crew.apps.manager import gateway_data_dir_obstruction

        root = tmp_path / "installed"
        root.mkdir()
        (root / "data").write_text("stands in for a link Windows cannot follow\n", encoding="utf-8")
        real_stat = os.stat

        def _unfollowable(path, *args, **kwargs):
            if (
                isinstance(path, (str, os.PathLike))
                and Path(path).name == "data"
                and kwargs.get("follow_symlinks", True)
            ):
                raise PermissionError(5, "Access is denied", str(path))
            return real_stat(path, *args, **kwargs)

        with monkeypatch.context() as scoped:
            scoped.setattr(os, "stat", _unfollowable)
            assert gateway_data_dir_obstruction(root) == _DATA_IS_A_FILE
            assert _data_dir_mkdir_accepts(root / "data") is False  # the same host verdict

    @requires_symlinks
    def test_a_root_data_link_is_refused_whatever_it_resolves_to(self, tmp_path, monkeypatch):
        """A link at `data` is refused by the preview whether it dangles or resolves
        to an in-tree directory -- the same answer the install gives, on every
        host: the gateway moves `data` aside and back across updates, and a relative
        link relocated out of its tree dangles and loses the directory it named.
        The copy keeps the link as a link and the target directory stays where it
        is; nothing is judged by what the link resolves to."""
        from kiro_crew.apps.manager import InstalledTreeRefused, copy_app_tree_as_installed

        root = self._source(tmp_path)
        (root / "state").mkdir()
        (root / "state" / "kept.json").write_text('{"kept": true}', encoding="utf-8")
        (root / "data").symlink_to(Path("state"), target_is_directory=True)
        dest = tmp_path / "installed-linked"
        with pytest.raises(InstalledTreeRefused) as refused:
            copy_app_tree_as_installed(root, dest, data_preserved=False)
        assert str(refused.value) == _DATA_IS_A_LINK
        assert os.path.islink(dest / "data")  # the tree it refused, as copied: the link, unfollowed
        assert (root / "state" / "kept.json").read_text(encoding="utf-8") == '{"kept": true}'

        (root / "data").unlink()
        (root / "data").symlink_to(Path("nowhere"))
        with pytest.raises(InstalledTreeRefused) as refused:
            copy_app_tree_as_installed(root, tmp_path / "installed-dangling", data_preserved=False)
        assert str(refused.value) == _DATA_IS_A_LINK

    def test_preserved_data_awaits_reads_what_the_install_will_put_back(
        self, tmp_path, monkeypatch
    ):
        """The three things `install_app` / `update_app` restore over the copied
        `data/`: the installed app's own directory, one a default uninstall left
        behind (same place), and a crashed sibling's `.{name}-data-tmp` copy."""
        from kiro_crew.apps import manager as manager_mod
        from kiro_crew.apps.manager import preserved_data_awaits

        apps = tmp_path / "apps"
        apps.mkdir()
        monkeypatch.setattr(manager_mod, "apps_dir", lambda: apps)
        assert preserved_data_awaits("demo") is False  # a first install
        (apps / "demo").mkdir()
        assert preserved_data_awaits("demo") is False  # an orphaned partial copy, no data
        (apps / "demo" / "data").write_text("a file, not the directory\n", encoding="utf-8")
        assert preserved_data_awaits("demo") is False
        (apps / "demo" / "data").unlink()
        (apps / "demo" / "data").mkdir()
        assert preserved_data_awaits("demo") is True  # installed, or left by an uninstall
        shutil.rmtree(apps / "demo")
        (apps / ".demo-data-tmp").mkdir()
        assert preserved_data_awaits("demo") is True  # a crashed sibling's copy, restored

    @requires_symlinks
    def test_preserved_data_awaits_never_counts_a_link(self, tmp_path, monkeypatch):
        """A link at `data` (an install that predates the link refusal) is not a
        directory the gateway can move aside and put back, so nothing is preserved
        over the copy for it -- the preview keeps the source's own `data`, exactly
        as the update that refuses to move the link would have."""
        from kiro_crew.apps import manager as manager_mod
        from kiro_crew.apps.manager import preserved_data_awaits

        apps = tmp_path / "apps"
        (apps / "demo" / "state").mkdir(parents=True)
        monkeypatch.setattr(manager_mod, "apps_dir", lambda: apps)
        (apps / "demo" / "data").symlink_to(Path("state"), target_is_directory=True)
        assert (apps / "demo" / "data").is_dir()  # `is_dir` follows it; the predicate must not
        assert preserved_data_awaits("demo") is False
        (apps / ".demo-data-tmp").symlink_to(apps / "demo" / "state", target_is_directory=True)
        assert preserved_data_awaits("demo") is False

    @requires_symlinks
    def test_a_root_file_or_link_named_like_a_gateway_entry_is_removed_too(self, tmp_path):
        from kiro_crew.apps.manager import copy_app_tree_as_installed

        root = self._source(tmp_path)
        (root / "data").write_text("a regular file so named\n", encoding="utf-8")
        (root / ".app_secret").symlink_to(Path("server.py"))
        dest = tmp_path / "installed"

        copy_app_tree_as_installed(root, dest, data_preserved=True)

        assert not os.path.lexists(dest / "data")
        assert not os.path.lexists(dest / ".app_secret")
        assert (dest / "server.py").is_file()  # the link's target itself is untouched

    @requires_symlinks
    def test_it_is_the_install_copy_link_for_link(self, tmp_path):
        """What the gate meets in the preview is what `install_app` leaves: a
        structural in-tree link kept as a link, a link into a dropped name kept but
        dangling, an escaping link omitted, an absolute in-tree link rewritten, and a
        text that climbs above the root and re-enters by naming the checkout kept
        verbatim -- so it resolves to the CHECKOUT's file from the copy."""
        from kiro_crew.apps.manager import copy_app_tree_as_installed

        root = self._source(tmp_path)
        (root / "requirements.txt").symlink_to(Path("requirements") / "prod.txt")
        (root / "node_modules").mkdir()
        (root / "node_modules" / "req.txt").write_text("x\n", encoding="utf-8")
        (root / "dropped.txt").symlink_to(Path("node_modules") / "req.txt")
        outside = tmp_path / "outside.txt"
        outside.write_text("x\n", encoding="utf-8")
        (root / "escaping.txt").symlink_to(outside)
        (root / "absolute.txt").symlink_to(root / "requirements" / "prod.txt")
        (root / "climbing.txt").symlink_to(
            Path("..") / ".." / "app-sources" / "demo" / "requirements" / "prod.txt"
        )
        # Same depth as the checkout, as the real app directory is.
        dest = tmp_path / "apps" / "demo"

        copy_app_tree_as_installed(root, dest, data_preserved=True)

        assert (dest / "requirements.txt").resolve() == (dest / "requirements" / "prod.txt")
        assert os.path.islink(dest / "dropped.txt") and not (dest / "dropped.txt").exists()
        assert not os.path.lexists(dest / "escaping.txt")
        assert os.path.islink(dest / "absolute.txt") and not os.path.isabs(
            os.readlink(dest / "absolute.txt")
        )
        assert (dest / "absolute.txt").resolve() == (dest / "requirements" / "prod.txt")
        # Kept verbatim: from the copy it reaches the checkout, outside the copy.
        assert (dest / "climbing.txt").resolve() == (root / "requirements" / "prod.txt").resolve()
