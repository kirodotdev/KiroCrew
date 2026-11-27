"""A descriptor-pinned path walk and copy (``kiro_crew.pinned_fs``).

For a file a person named by path. Anything running as the same user can swap a
component of that path for a link between the check and the open, so a check by
name does not guard the open that follows it. :func:`pin_parent` opens the resolved
parent one ``O_NOFOLLOW`` component at a time, and :func:`copy_file_pinned` opens
the source through that descriptor and judges the opened inode, so the file that is
judged is the file whose bytes are copied.

The helpers hold no policy. They do not know which locations are protected, so ask
``is_sensitive_path`` first. Where the platform cannot pin
(:func:`supports_pinned_walk` is ``False``, as on Windows), refuse the path instead
of opening it by name.
"""

from __future__ import annotations

import os
from typing import Callable

from kiro_crew import pinned_fs

__all__ = [
    "PinnedPathRefusal",
    "copy_file_pinned",
    "fd_real_path",
    "is_reparse_point",
    "pin_parent",
    "supports_pinned_walk",
]

#: Raised by :func:`pin_parent` for a component that is a link or not a directory.
#: The gateway's own class, so ``except PinnedPathRefusal`` catches what it raises.
#: It subclasses ``Exception``, not ``OSError``.
PinnedPathRefusal = pinned_fs.PinnedPathRefusal


def _ignore_skip(_reason: str, _path: str) -> None:
    return None


def supports_pinned_walk() -> bool:
    """Whether this platform can open a path relative to a directory descriptor.

    ``kiro_crew.pinned_fs.supports_pinned_walk``: it needs ``O_DIRECTORY``,
    ``O_NOFOLLOW`` and ``dir_fd`` support, so it is ``False`` on Windows. When it is
    ``False``, refuse a path the person named: :func:`pin_parent` cannot run there,
    and a by-name open would follow a link planted after the check.
    """
    return bool(pinned_fs.supports_pinned_walk())


def is_reparse_point(path: str | os.PathLike[str]) -> bool:
    """Whether *path* itself is a symlink or, on Windows, any reparse point.

    ``kiro_crew.pinned_fs.is_reparse_point``. On POSIX it answers as
    ``is_link_or_junction`` does. On Windows it is wider: any non-zero
    ``st_reparse_tag`` counts, not only a symlink or a junction. It screens a NAME at
    one instant, so it is a cheap first refusal, not the guard: pair it with
    :func:`pin_parent` and :func:`copy_file_pinned`, which judge the opened inode.
    """
    return bool(pinned_fs.is_reparse_point(os.fspath(path)))


def pin_parent(resolved_parent: str, *, what: str) -> int:
    """An open descriptor for the directory *resolved_parent*, walked without following a link.

    ``kiro_crew.pinned_fs.pin_parent``: one ``openat`` per component, each relative
    to the previous one and each with ``O_NOFOLLOW``, so a component swapped for a
    link after the path was resolved is refused, and every component already open
    stays fixed. Resolve *resolved_parent* once yourself (``os.path.realpath``)
    before the call; a path still holding a link is refused too. *what* names the
    target in the refusal message.

    Raises :data:`PinnedPathRefusal` for a component that is a link or not a
    directory; any other ``OSError`` propagates. Close the returned descriptor. Call
    it only when :func:`supports_pinned_walk` is ``True``.
    """
    return pinned_fs.pin_parent(resolved_parent, what=what)


def copy_file_pinned(
    src: str,
    dst: str,
    *,
    dir_fd: int | None = None,
    name: str | None = None,
    force_mode: int | None = None,
    max_bytes: int | None = None,
    expected_src_ident: tuple[int, int] | None = None,
    on_skip: Callable[[str, str], None] | None = None,
    on_created: Callable[[os.stat_result], None] | None = None,
) -> bool:
    """Copy one file's bytes from a descriptor that is judged before a byte is read.

    ``kiro_crew.pinned_fs.copy_file_pinned``. ``True`` when the bytes were copied.
    ``False`` when the source was refused, with the reason handed to
    ``on_skip(reason, src)``:

    * ``"symlink"``: a link at the final name (``"identity_changed"`` when
      *expected_src_ident* is given, since the name was judged a file before);
    * ``"not_regular"``: a hardlinked or non-regular file;
    * ``"identity_changed"``: the opened inode's ``(st_dev, st_ino)`` is not
      *expected_src_ident*, the identity your own check observed;
    * ``"too_large"``: over *max_bytes*, refused before the destination is created,
      or while copying for a source that grows, which leaves the destination empty.

    Pass *dir_fd* (from :func:`pin_parent`) and *name* (the final component) to open
    the source through the pinned parent; without them *src* is opened by name.
    *src* names the source in the report either way. *dst* is created by name with
    ``O_EXCL``, so point it into a directory you just created: an existing *dst*
    raises ``FileExistsError`` and is never overwritten. The copy takes the source's
    mode, or *force_mode*, and its timestamps. ``on_created`` receives the
    destination's ``fstat`` once the copy is complete: the identity to match when
    you reopen the copy by name. ``FileNotFoundError`` and every other ``OSError``
    propagate, and a copy that fails part way leaves its destination empty.

    It holds no policy and does not ask the sensitive-path gate, so check the path
    with ``is_sensitive_path`` first. Windows has no ``O_NOFOLLOW`` and no ``dir_fd``,
    so a by-name source there follows a final link: refuse the path when
    :func:`supports_pinned_walk` is ``False``.
    """
    return bool(
        pinned_fs.copy_file_pinned(
            src,
            dst,
            dir_fd=dir_fd,
            name=name,
            force_mode=force_mode,
            max_bytes=max_bytes,
            expected_src_ident=expected_src_ident,
            on_skip=on_skip if on_skip is not None else _ignore_skip,
            on_created=on_created,
        )
    )


def fd_real_path(fd: int) -> str | None:
    """The real path of the open descriptor *fd*, or ``None`` when it cannot be read.

    ``kiro_crew.pinned_fs.fd_real_path``: the kernel's own name for the inode already
    open (``/proc/self/fd`` on Linux, ``F_GETPATH`` on macOS,
    ``GetFinalPathNameByHandleW`` on Windows), so no link is left in it to swap. Use
    it to check where an ``O_NOFOLLOW`` open really landed. It fails closed with
    ``None``, never a by-name answer, so treat ``None`` as a refusal.
    """
    return pinned_fs.fd_real_path(fd)
