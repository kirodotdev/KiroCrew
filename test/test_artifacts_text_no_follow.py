"""The artifact store's reads and writes must not follow a planted link.

The store root is ``config_dir() / "artifacts"``, which is in none of the
sandbox's three crew-home dispositions, so an in-sandbox process can plant a
link in the tree these readers and writers walk. Three shapes are covered,
deliberately, because they fail differently:

* a SYMLINK at the leaf — the pre-open ``realpath`` removes it, so the leaf open
  is what has to see it before the resolution happens. POSIX-gated: planting one
  on Windows needs ``SeCreateSymbolicLinkPrivilege``.
* a HARDLINK at the leaf — invisible to every path-based guard (``realpath``
  yields the alias's own name, ``is_symlink()`` is ``False``), so only a
  descriptor check closes it. Runs on Windows too, where no privilege is needed.
* a FIFO at the leaf — a blocking ``O_RDONLY`` open waits for a writer, and
  callers reach this inline on the gateway event loop, so the check has to happen
  on a non-blocking open. POSIX-gated: ``mkfifo``.
"""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

import pytest

from kiro_crew.artifacts import ArtifactError, ArtifactStore


@pytest.fixture
def store(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore(root=tmp_path / "artifacts")


def _plant_symlink(link: Path, target: Path) -> None:
    """Replace *link* with a symlink to *target* (the swap an attacker wins)."""
    link.parent.mkdir(parents=True, exist_ok=True)
    link.unlink(missing_ok=True)
    link.symlink_to(target)


def _symlink_capable() -> bool:
    """Whether this host can plant a symlink at all.

    Windows needs ``SeCreateSymbolicLinkPrivilege`` (Developer Mode or an elevated
    process); without it ``os.symlink`` raises ``WinError 1314``.
    """
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "target").write_text("x", encoding="utf-8")
        try:
            (root / "link").symlink_to(root / "target")
        except OSError:
            return False
    return True


needs_symlink = pytest.mark.skipif(
    not _symlink_capable(),
    reason="planting a symlink needs SeCreateSymbolicLinkPrivilege on Windows",
)

needs_fifo = pytest.mark.skipif(
    not hasattr(os, "mkfifo"),
    reason="mkfifo is POSIX-only",
)

needs_dir_fd = pytest.mark.skipif(
    not (
        getattr(os, "O_DIRECTORY", 0)
        and os.open in getattr(os, "supports_dir_fd", set())
        and os.rename in getattr(os, "supports_dir_fd", set())
    ),
    reason="the pinned parent needs POSIX dir_fd support",
)


def _plant_hardlink(link: Path, target: Path) -> None:
    """Replace *link* with a second name for *target*'s inode."""
    link.parent.mkdir(parents=True, exist_ok=True)
    link.unlink(missing_ok=True)
    os.link(target, link)


class TestTextReadRefusesALinkAtTheLeaf:
    @needs_symlink
    def test_read_text_refuses_a_symlinked_leaf(self, store: ArtifactStore, tmp_path: Path) -> None:
        """A link at the leaf is refused rather than resolved to its target.

        The path handed to the no-follow open must be the UNRESOLVED leaf: a
        ``realpath`` first would strip the link, and the open would meet the
        ordinary file it points at.
        """
        secret = tmp_path / "secret.txt"
        secret.write_text("not-for-artifacts", encoding="utf-8")
        leaf = store.root / "demo" / "current.html"
        _plant_symlink(leaf, secret)

        with pytest.raises(ArtifactError):
            store._read_text(leaf)

    def test_read_text_refuses_a_path_outside_the_store_root(
        self, store: ArtifactStore, tmp_path: Path
    ) -> None:
        """A resolved path outside the root is refused without needing a link.

        This is the containment half of the pair, and it is the part that runs on
        every platform: it catches the ancestor-swap case a leaf open cannot see,
        by comparing the resolved path against the store root.
        """
        outside = tmp_path / "outside.txt"
        outside.write_text("not-in-the-store", encoding="utf-8")

        with pytest.raises(ArtifactError):
            store._read_text(outside)

    def test_read_text_refuses_a_hardlinked_leaf(
        self, store: ArtifactStore, tmp_path: Path
    ) -> None:
        """A hardlink has no path tell, so only a descriptor check can refuse it."""
        victim = tmp_path / "victim.txt"
        victim.write_text("not-for-artifacts", encoding="utf-8")
        leaf = store.root / "demo" / "current.html"
        _plant_hardlink(leaf, victim)

        with pytest.raises(ArtifactError):
            store._read_text(leaf)

    @needs_fifo
    def test_read_text_refuses_a_fifo_without_blocking(self, store: ArtifactStore) -> None:
        """A FIFO at the leaf is refused on a non-blocking open.

        A blocking ``O_RDONLY`` open of a FIFO waits for a writer that never
        comes, and callers reach this reader inline on the gateway event loop, so
        a planted FIFO stalls the whole gateway. ``open_file_no_reparse`` carries
        ``nonblocking`` for exactly this: the open returns, ``fstat`` shows a
        non-regular file, and the refusal happens before anyone waits.
        """
        leaf = store.root / "demo" / "current.html"
        leaf.parent.mkdir(parents=True, exist_ok=True)
        os.mkfifo(leaf)

        started = time.monotonic()
        with pytest.raises(ArtifactError):
            store._read_text(leaf)
        # Far below any plausible FIFO writer wait; a regression here hangs.
        assert time.monotonic() - started < 5.0

    def test_read_text_refuses_an_outside_path_by_descriptor(
        self, store: ArtifactStore, tmp_path: Path
    ) -> None:
        """Containment is judged on the opened inode, not the pre-open path.

        ``realpath`` runs before the open, so an ancestor swapped for a link in
        that window redirects the traversal while the pre-open check keeps
        validating a path the descriptor does not refer to. The descriptor's own
        path is what the store compares against the root.
        """
        outside = tmp_path / "elsewhere.html"
        outside.write_text("not-in-the-store", encoding="utf-8")

        with pytest.raises(ArtifactError):
            store._read_text(outside)

    def test_read_text_still_reads_a_plain_file(self, store: ArtifactStore) -> None:
        """The refusal is about the link, not about reading at all."""
        leaf = store.root / "demo" / "current.html"
        leaf.parent.mkdir(parents=True, exist_ok=True)
        leaf.write_text("<p>hello</p>", encoding="utf-8")

        assert store._read_text(leaf) == "<p>hello</p>"

    def test_read_text_has_no_size_ceiling(self, store: ArtifactStore) -> None:
        """The reader stays uncapped, so a snapshot cannot truncate its source.

        ``snapshot`` on a linked file mirrors what it read back to the source. A
        bounded read would therefore become a truncating write of the user's own
        file, which is why this path reads the whole body rather than going
        through the general helper's ceiling.
        """
        leaf = store.root / "demo" / "big.html"
        leaf.parent.mkdir(parents=True, exist_ok=True)
        body = "x" * (26_214_400 + 1024)  # past MAX_CONTENT_BYTES
        leaf.write_text(body, encoding="utf-8")

        assert len(store._read_text(leaf)) == len(body)

    def test_read_text_a_missing_file_stays_a_FileNotFoundError(self, store: ArtifactStore) -> None:
        """A vanished file must not be collapsed into the link refusal.

        Callers around this store distinguish the two: a file that disappeared
        between a listing snapshot and the read is a skip-and-warn, while a link
        at the leaf is a refusal. ``ArtifactError`` is not an ``OSError``, so
        collapsing them would hide a disappeared file from every
        ``except OSError`` / ``except FileNotFoundError`` handler.
        """
        with pytest.raises(FileNotFoundError):
            store._read_text(store.root / "demo" / "never-existed.html")


class TestTextWriteRefusesALinkAtTheLeaf:
    @needs_symlink
    def test_write_text_refuses_a_symlinked_leaf(
        self, store: ArtifactStore, tmp_path: Path
    ) -> None:
        """An overwrite through a planted link rewrites a file the caller never authorized.

        The store writes ``current.html`` on every content update, so this is the
        ordinary path, not an exotic one.
        """
        victim = tmp_path / "victim.txt"
        victim.write_text("original", encoding="utf-8")
        leaf = store.root / "demo" / "current.html"
        _plant_symlink(leaf, victim)

        with pytest.raises(ArtifactError):
            store._write_text(leaf, "overwritten")

        assert victim.read_text(encoding="utf-8") == "original"

    def test_write_text_refuses_a_path_outside_the_store_root(
        self, store: ArtifactStore, tmp_path: Path
    ) -> None:
        """Containment runs on every platform, link privilege or not."""
        outside = tmp_path / "outside.txt"
        outside.write_text("untouched", encoding="utf-8")

        with pytest.raises(ArtifactError):
            store._write_text(outside, "overwritten")

        assert outside.read_text(encoding="utf-8") == "untouched"

    def test_write_text_refuses_a_hardlinked_leaf(
        self, store: ArtifactStore, tmp_path: Path
    ) -> None:
        """A hardlinked leaf would be truncated in place by the staged replace."""
        victim = tmp_path / "victim.txt"
        victim.write_text("original", encoding="utf-8")
        leaf = store.root / "demo" / "current.html"
        _plant_hardlink(leaf, victim)

        with pytest.raises(ArtifactError):
            store._write_text(leaf, "overwritten")

        assert victim.read_text(encoding="utf-8") == "original"

    @needs_symlink
    def test_write_text_never_follows_a_link_at_the_staging_name(
        self, store: ArtifactStore, tmp_path: Path
    ) -> None:
        """A link at a plausible staging name is neither followed nor clobbered.

        The stage name is unique per call and created with ``O_EXCL``, so a name
        planted in advance cannot collide with it. This asserts the property
        rather than a predicted filename: the write succeeds, the planted link
        and its target are untouched, and the stage cleans up after itself.
        """
        victim = tmp_path / "victim.txt"
        victim.write_text("original", encoding="utf-8")
        leaf = store.root / "demo" / "current.html"
        leaf.parent.mkdir(parents=True, exist_ok=True)
        planted = leaf.with_suffix(leaf.suffix + ".tmp")
        _plant_symlink(planted, victim)

        store._write_text(leaf, "written")

        assert victim.read_text(encoding="utf-8") == "original"
        assert planted.is_symlink()
        assert store._read_text(leaf) == "written"
        # The planted name is left exactly as found, and the stage adds no
        # second, stray temp beside it.
        assert sorted(p.name for p in leaf.parent.iterdir()) == [
            "current.html",
            "current.html.tmp",
        ]

    @needs_symlink
    @needs_dir_fd
    def test_write_text_refuses_a_swapped_ancestor_directory(
        self, store: ArtifactStore, tmp_path: Path
    ) -> None:
        """A parent swapped for a link after the pre-open check cannot redirect the write.

        The stage and the rename both address their names as strings in the
        obvious implementation, and ``O_NOFOLLOW`` only guards the final
        component, so every ancestor is re-walked at each step. Holding an
        ``O_DIRECTORY|O_NOFOLLOW`` handle on the parent across both steps is what
        closes it.
        """
        victim_dir = tmp_path / "victim"
        victim_dir.mkdir()
        (victim_dir / "current.html").write_text("ORIGINAL", encoding="utf-8")

        target_dir = store.root / "demo"
        target_dir.mkdir(parents=True, exist_ok=True)
        leaf = target_dir / "current.html"
        leaf.write_text("store-content", encoding="utf-8")

        # Swap the parent for a link to the victim directory.
        target_dir.rename(store.root / "demo-real")
        target_dir.symlink_to(victim_dir)

        with pytest.raises((ArtifactError, OSError)):
            store._write_text(leaf, "overwritten")

        assert (victim_dir / "current.html").read_text(encoding="utf-8") == "ORIGINAL"

    def test_write_text_still_creates_a_missing_file(self, store: ArtifactStore) -> None:
        """Creating stays supported: ``meta.json`` and ``v{n}.html`` rely on it."""
        leaf = store.root / "demo" / "meta.json"
        store._write_text(leaf, "{}")

        assert leaf.read_text(encoding="utf-8") == "{}"

    def test_write_text_still_overwrites_a_plain_file(self, store: ArtifactStore) -> None:
        """The create-or-overwrite contract is unchanged for an ordinary file."""
        leaf = store.root / "demo" / "current.html"
        leaf.parent.mkdir(parents=True, exist_ok=True)
        leaf.write_text("first", encoding="utf-8")

        store._write_text(leaf, "second")

        assert leaf.read_text(encoding="utf-8") == "second"
