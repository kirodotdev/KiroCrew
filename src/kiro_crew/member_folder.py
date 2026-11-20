"""A member's folder under the data home, opened without following links.

``members/<slug>`` holds the files the gateway keeps for one member. The ``members``
folder is opened without following a link and held; a link there is resolved once, as the
member dashboard readers do, and the folder it names is held instead. The slug level is
then opened relative to the held folder and never through a link, and a file is written or
claimed by descriptor, so a link at ``members/<slug>`` or at a file is refused rather
than followed. A link at ``members`` leads to the folder it points to.
Where the platform cannot open relative to a descriptor the same levels are checked with
``lstat`` and the files are addressed by name.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

from kiro_crew import members
from kiro_crew.atomic_write import atomic_write, atomic_write_at
from kiro_crew.pinned_fs import dir_flags, open_pinned_descendant_dir, supports_pinned_walk


@dataclass(frozen=True)
class MemberFolder:
    """An open member folder: *fd* is ``None`` where the platform cannot pin one.

    *parent_fd* is the open ``members`` folder above it, or ``None`` where *fd* is.
    *members_is_link* is ``True`` when ``members`` itself is a link: the folder it names
    is somewhere else, and a caller leaves its mode as it is.
    """

    fd: int | None
    path: Path
    parent_fd: int | None = None
    members_is_link: bool = False

    def leaf(self, name: str) -> str | Path:
        """What to hand a call that also takes ``dir_fd=self.fd`` for file *name*."""
        return name if self.fd is not None else self.path / name

    def refuse_link(self, name: str) -> None:
        """Raise :class:`members.MemberSlugError` when file *name* is a link."""
        try:
            linked = stat.S_ISLNK(
                os.stat(self.leaf(name), dir_fd=self.fd, follow_symlinks=False).st_mode
            )
        except FileNotFoundError:
            linked = False
        if linked:
            raise members.MemberSlugError(f"refusing {name!r}: it is a link")

    def write(self, name: str, text: str) -> None:
        """Replace file *name* with *text*; a link at *name* is refused, not replaced."""
        self.refuse_link(name)
        if self.fd is None:
            atomic_write(self.path / name, text, restrict_to_owner=True)
        else:
            atomic_write_at(self.fd, name, text, mode=0o600)


@contextmanager
def open_member_folder(slug: str) -> Iterator[MemberFolder]:
    """Open ``members/<slug>``, creating it owner-only; a link at the slug level is refused.

    ``members`` is opened without following a link and held. When that open finds a link
    there, the link is resolved once, as the member dashboard readers do, the folder it
    names is opened the same way and held, and ``members_is_link`` is set: the write
    stays in that folder and a caller leaves its mode as it is. A link that leads to no
    folder that can be opened (a dangling link, a loop, a file, a folder it may not open,
    a target that is not text), or to the data home or a folder above it, is refused, and
    nothing is created behind it. The slug level is created relative to the held folder,
    so nothing is made behind a link.

    Everything that happens before the folder is yielded either succeeds or ends in
    :class:`members.MemberSlugError`, whatever the cause (the data home, a link, a level
    that cannot be opened or written, a folder removed meanwhile). Its message names no
    path, and every descriptor opened so far is closed. What the caller's own body raises
    inside the ``with`` passes through unchanged.
    """
    with ExitStack() as stack:
        try:
            folder = _open_folder(slug, stack)
        except members.MemberSlugError:
            raise
        except Exception as exc:
            raise members.MemberSlugError(
                "refusing to use the member folder: it cannot be opened"
            ) from exc
        yield folder


def _open_folder(slug: str, stack: ExitStack) -> MemberFolder:
    """Open both levels, registering every descriptor on *stack*; see the caller."""
    members.validate_slug(slug)
    named = members.members_root()
    base = named.parent.resolve()
    base.mkdir(parents=True, exist_ok=True)
    refusal = members.MemberSlugError
    try:
        parent_fd = stack.enter_context(
            open_pinned_descendant_dir(
                base, (named.name,), what="member folder", create=True, refusal=refusal
            )
        )
        root = base / named.name
        members_is_link = False
    except refusal as exc:
        if supports_pinned_walk():
            parent_fd = _open_link_at_members(base, named.name, stack)
            root = named
        else:
            parent_fd, root = _open_link_at_members_by_name(named, base, exc, stack)
        members_is_link = True
    path = root / slug
    if parent_fd is None:
        # No descriptors here: the whole chain is checked by ``lstat`` instead.
        try:
            stack.enter_context(
                open_pinned_descendant_dir(
                    root.parent,
                    (root.name, slug),
                    what="member folder",
                    create=True,
                    refusal=refusal,
                )
            )
        except refusal as exc_walk:
            raise refusal(
                "refusing to use the member folder: the folder cannot be used"
            ) from exc_walk
        return MemberFolder(None, path, None, members_is_link)
    # The slug level is opened relative to the held ``members`` descriptor, so both
    # levels stay held and neither is looked up by name again.
    with suppress(FileExistsError):
        os.mkdir(slug, 0o700, dir_fd=parent_fd)
    try:
        fd = os.open(slug, dir_flags(), dir_fd=parent_fd)
    except OSError as exc_slug:
        raise refusal(
            f"refusing to use the member folder: {slug!r} is a link or not a directory"
        ) from exc_slug
    stack.callback(os.close, fd)
    return MemberFolder(fd, path, parent_fd, members_is_link)


def _open_link_at_members(base: Path, name: str, stack: ExitStack) -> int:
    """Open what the link at *name* in the data home *base* names, by descriptor, and vet it.

    The link is opened once, relative to the held data home, so no name is used after the
    screen. What was opened is vetted by identity (``fstat``) against the data home and every
    folder above it. A link whose text is not text is refused: the text is read once, relative
    to the same descriptor, and a race can only change that check, not what is opened.
    """
    refusal = members.MemberSlugError
    base_fd = os.open(base, dir_flags())
    try:
        if not stat.S_ISLNK(os.stat(name, dir_fd=base_fd, follow_symlinks=False).st_mode):
            raise refusal("refusing to use the member folder: members is a link or not a directory")
        try:
            os.readlink(name, dir_fd=base_fd).encode("utf-8")
            fd = os.open(name, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0), dir_fd=base_fd)
        except (OSError, UnicodeError) as exc:
            # A link that names itself, a loop, more than 40 links in a row, one that leads
            # nowhere, a file, a folder that may not be opened, or a target that is not text.
            raise refusal(
                "refusing to use the member folder: the link at members cannot be resolved"
            ) from exc
        stack.callback(os.close, fd)
        if _is_data_home_or_above(fd, base_fd):
            # The slug folders are made inside the folder ``members`` names: the data
            # home, or a folder above it, would put them beside the stores kept there.
            raise refusal(
                "refusing to use the member folder: the link at members leads to the "
                "data home or to a folder above it"
            )
        return fd
    finally:
        os.close(base_fd)


def _is_data_home_or_above(fd: int, base_fd: int) -> bool:
    """Whether *fd* is the folder *base_fd* or one of the folders above it, by identity.

    Each level is read with ``stat`` on ``..`` chained from *base_fd*, which needs search
    permission on the folders passed through and never read permission (an ``open`` of an
    ancestor would, and a host with ``/home`` at 0711 would then refuse a good link).
    """
    wanted = os.fstat(fd)
    rel = "."
    seen = None
    while True:
        here = os.stat(rel, dir_fd=base_fd)
        key = (here.st_dev, here.st_ino)
        if key == (wanted.st_dev, wanted.st_ino):
            return True
        if key == seen:
            return False
        seen = key
        rel = ".." if rel == "." else f"{rel}/.."


def _open_link_at_members_by_name(
    named: Path, base: Path, walk_refusal: Exception, stack: ExitStack
) -> tuple[int | None, Path]:
    """Where the platform cannot open relative to a descriptor: resolve the link by name.

    shortcut: this arm still screens the name and then uses it (a declared site), because
    there is no descriptor to hold; upgrade when such a platform gains ``dir_fd``.
    """
    refusal = members.MemberSlugError
    # ``is_symlink`` is False for a Windows junction, which the walk refuses as well.
    if not (named.is_symlink() or named.is_junction()):
        raise refusal(
            "refusing to use the member folder: members is a link or not a directory"
        ) from walk_refusal
    try:
        root = named.resolve()
        # A name that is not text (an invalid byte) cannot be handed on or logged.
        str(root).encode("utf-8")
    except (OSError, RuntimeError, UnicodeError) as exc_resolve:
        raise refusal(
            "refusing to use the member folder: the link at members cannot be resolved"
        ) from exc_resolve
    if root == base or root in base.parents:
        raise refusal(
            "refusing to use the member folder: the link at members leads to the "
            "data home or to a folder above it"
        ) from None
    try:
        parent_fd = stack.enter_context(
            open_pinned_descendant_dir(
                root.parent, (root.name,), what="member folder", refusal=refusal
            )
        )
    except refusal as exc_open:
        raise refusal(
            "refusing to use the member folder: the link at members does not lead "
            "to a folder that can be opened"
        ) from exc_open
    return parent_fd, root
