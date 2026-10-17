# SPDX-License-Identifier: Apache-2.0
"""Disk safety for every artifact kind: the store's own write guarantees.

One test per row of the dashboard plan's disk-safety table that is the STORE's
to keep, not any one kind's. Nothing here knows what a dashboard is: the rows
hold for a widget, a report, an image sidecar and a dashboard package alike,
because they are about the bytes reaching the disk at all.

Each case is written to fail against the store as it stands, so the guarantee is
observable rather than asserted.
"""

from __future__ import annotations

import errno
import os
from pathlib import Path
from typing import Any

import pytest

from kiro_crew import artifacts
from kiro_crew.artifacts import ArtifactError, ArtifactStore


@pytest.fixture()
def store(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore(root=tmp_path / "artifacts")


class TestATornWriteIsNotObservable:
    """`atomic_write`: temp file + fsync + rename, for every file the store writes.

    tmp + rename alone makes the SWAP atomic but not the DATA durable: the
    rename can land while the bytes are still only in the page cache, so a crash
    leaves the artifact pointing at a file the kernel never finished. The fsync
    is what makes "the rename published real bytes" true rather than likely.
    """

    def test_the_content_file_is_fsynced_before_the_rename(
        self, store: ArtifactStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        order: list[tuple[str, Any]] = []
        real_fsync = os.fsync
        real_replace = os.replace

        def spy_fsync(fd: int) -> None:
            order.append(("fsync", fd))
            real_fsync(fd)

        def spy_replace(src: Any, dst: Any, **kw: Any) -> None:
            order.append(("replace", str(dst)))
            real_replace(src, dst, **kw)

        monkeypatch.setattr(os, "fsync", spy_fsync)
        monkeypatch.setattr(os, "replace", spy_replace)
        store.create(name="Report", kind="widget", content="<p>hi</p>")

        renames = [i for i, (what, _) in enumerate(order) if what == "replace"]
        syncs = [i for i, (what, _) in enumerate(order) if what == "fsync"]
        assert renames, "the store published nothing through a rename"
        assert syncs, "no file the store wrote was fsynced"
        # Every publish has a sync BEFORE it, which is the ordering that matters:
        # a sync after the rename does not make the published bytes durable.
        for publish in renames:
            assert any(s < publish for s in syncs), (
                f"the rename at step {publish} published bytes that were never synced; "
                f"sequence was {[w for w, _ in order]}"
            )


class TestDiskFullFailsClosed:
    """A full disk leaves the last good version in place and says so.

    The rule has three halves and the leftover temp file is the one a tmp+rename
    writer gets wrong: the write raises, the rename never runs, and the partial
    temp file stays on the disk that is already full -- so the next write finds
    less room than this one did, and a reader listing the directory sees a file
    the store does not account for.
    """

    def _fill_disk_on_write(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Make every store file write raise ENOSPC, as a full disk does.

        Injected at the store's OWN seam: ``_prepare_store_write`` returns the
        opener its writers hand to ``open``, so an opener that raises is exactly
        what a writer meets when the filesystem has no room for the temp file.
        Creating the parent directory still happens, because a full disk does not
        un-create what is already there.
        """
        real_prepare = artifacts._prepare_store_write

        def refusing_prepare(resolved: Path, *, owner_only: bool) -> Any:
            real_prepare(resolved, owner_only=owner_only)

            def opener(path: str, flags: int) -> int:
                raise OSError(errno.ENOSPC, "No space left on device")

            return opener

        monkeypatch.setattr(artifacts, "_prepare_store_write", refusing_prepare)

    def test_an_existing_artifact_keeps_its_last_content(
        self, store: ArtifactStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        art = store.create(name="Report", kind="widget", content="<p>good</p>")
        # Scoped to the failing write: the read that follows has to run against
        # a store whose writers work, and the autouse fixtures this test
        # inherits stay patched either way.
        with monkeypatch.context() as full_disk:
            self._fill_disk_on_write(full_disk)
            with pytest.raises((ArtifactError, OSError)):
                store.update(art.slug, content="<p>doomed</p>")
        assert "good" in store.get(art.slug).content

    def test_no_partial_temp_file_is_left_behind(
        self, store: ArtifactStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The half a tmp+rename writer gets wrong. A temp file left on a full
        disk takes room the next write needs and shows up in a listing as a file
        the store does not account for."""
        art = store.create(name="Report", kind="widget", content="<p>good</p>")
        with monkeypatch.context() as full_disk:
            self._fill_disk_on_write(full_disk)
            with pytest.raises((ArtifactError, OSError)):
                store.update(art.slug, content="<p>doomed</p>")
        leftovers = sorted(
            str(p.relative_to(store._root)) for p in store._root.rglob("*.tmp") if p.is_file()
        )
        assert leftovers == [], f"the failed write left temp files behind: {leftovers}"

    def test_the_failure_names_the_disk_and_not_the_content(
        self, store: ArtifactStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The owner has to be told which it is. "your content is invalid" sends
        someone editing a page that was fine; a full disk needs space freed."""
        art = store.create(name="Report", kind="widget", content="<p>good</p>")
        with monkeypatch.context() as full_disk:
            self._fill_disk_on_write(full_disk)
            with pytest.raises((ArtifactError, OSError)) as caught:
                store.update(art.slug, content="<p>doomed</p>")
        said = str(caught.value).lower()
        assert "space" in said or "disk" in said, f"the refusal did not name the disk: {said!r}"


class TestAFailedWritebackDoesNotPublish:
    """The sync's own failure is the one that decides whether the rename runs.

    A filesystem with delayed allocation does not report a full disk or a failing
    device at ``write`` -- it reports it at ``fsync``, after the writer has
    already handed over every byte. If that error is treated as advisory the
    rename still runs, so the artifact's name moves to a file the device never
    took, the call returns success, and the last good version is gone.

    The one error that may be ignored says the platform has no sync for this
    descriptor, which is not a claim about the data.
    """

    def _fsync_raises(self, monkeypatch: pytest.MonkeyPatch, code: int, text: str) -> None:
        def refusing_fsync(fd: int) -> None:
            raise OSError(code, text)

        monkeypatch.setattr(os, "fsync", refusing_fsync)

    def test_a_full_disk_reported_at_sync_keeps_the_last_version(
        self, store: ArtifactStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        art = store.create(name="Report", kind="widget", content="<p>good</p>")
        with monkeypatch.context() as sync_fails:
            self._fsync_raises(sync_fails, errno.ENOSPC, "No space left on device")
            with pytest.raises(OSError):
                store.update(art.slug, content="<p>doomed</p>")
        assert "good" in store.get(art.slug).content

    def test_a_failing_device_reported_at_sync_keeps_the_last_version(
        self, store: ArtifactStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """EIO is the other half: the disk has room and still did not take the
        bytes. Both are writeback failures and neither may publish."""
        art = store.create(name="Report", kind="widget", content="<p>good</p>")
        with monkeypatch.context() as sync_fails:
            self._fsync_raises(sync_fails, errno.EIO, "Input/output error")
            with pytest.raises(OSError):
                store.update(art.slug, content="<p>doomed</p>")
        assert "good" in store.get(art.slug).content

    def test_no_partial_temp_file_is_left_when_the_sync_fails(
        self, store: ArtifactStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A writeback error at ``fsync`` skips the rename, so the ``.tmp`` the
        write opened must be unlinked rather than abandoned -- a full disk is the
        worst place to leave a stray file."""
        art = store.create(name="Report", kind="widget", content="<p>good</p>")
        with monkeypatch.context() as sync_fails:
            self._fsync_raises(sync_fails, errno.ENOSPC, "No space left on device")
            with pytest.raises(OSError):
                store.update(art.slug, content="<p>doomed</p>")
        leftover = [
            str(p.relative_to(store._root)) for p in store._root.rglob("*.tmp") if p.is_file()
        ]
        assert not leftover, f"a .tmp was left after the sync failed: {leftover}"

    def test_a_platform_without_fsync_still_publishes(
        self, store: ArtifactStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Refusing here would make the store unusable wherever a descriptor
        cannot be synced, and the rename's atomicity holds without it."""
        art = store.create(name="Report", kind="widget", content="<p>old</p>")
        with monkeypatch.context() as no_sync:
            self._fsync_raises(no_sync, errno.EINVAL, "Invalid argument")
            store.update(art.slug, content="<p>new</p>")
        assert "new" in store.get(art.slug).content


class TestAWriteNeverLandsThroughALink:
    """`pinned_fs` link refusal: a store write goes where its path names, or nowhere.

    This row already holds; the case is here because the row is worth a test of
    its own rather than an inference from the path-resolution suite. A store
    write resolves its path and asks the sensitive-path fence about the REALPATH,
    so a symlink planted at an artifact's directory or at a file inside it cannot
    redirect the write to somewhere the path does not name.
    """

    def test_a_symlinked_artifact_directory_cannot_redirect_a_write(
        self, store: ArtifactStore, tmp_path: Path
    ) -> None:
        art = store.create(name="Report", kind="widget", content="<p>one</p>")
        art_dir = store._root / art.slug
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        # Replace the artifact's own directory with a link to a directory outside
        # the store. A write that followed it would publish the artifact's bytes
        # where its path does not point.
        for child in sorted(art_dir.iterdir()):
            child.replace(elsewhere / child.name)
        art_dir.rmdir()
        try:
            art_dir.symlink_to(elsewhere, target_is_directory=True)
        except (OSError, NotImplementedError):  # pragma: no cover - platform dependent
            pytest.skip("this platform does not let the test plant a directory symlink")

        # The store refuses outright: with the link in place the slug resolves
        # outside the root, which it checks before it writes anything.
        with pytest.raises((ArtifactError, OSError)):
            store.update(art.slug, content="<p>two</p>")
        assert (elsewhere / "current.html").read_text(encoding="utf-8") == "<p>one</p>"

    def test_a_symlinked_content_file_is_not_written_through(
        self, store: ArtifactStore, tmp_path: Path
    ) -> None:
        """The file-level half. A link planted at ``current.html`` must not send
        the next write to the link's target."""
        art = store.create(name="Report", kind="widget", content="<p>one</p>")
        target = tmp_path / "outside.html"
        target.write_text("untouched", encoding="utf-8")
        content_file = store._root / art.slug / "current.html"
        content_file.unlink()
        try:
            content_file.symlink_to(target)
        except (OSError, NotImplementedError):  # pragma: no cover - platform dependent
            pytest.skip("this platform does not let the test plant a file symlink")

        try:
            store.update(art.slug, content="<p>two</p>")
        except (ArtifactError, OSError):
            pass
        assert (
            target.read_text(encoding="utf-8") == "untouched"
        ), "the write followed a link and landed outside the store"
