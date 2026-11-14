"""Removing stale ``*.json`` files from an overlay folder, never through a link.

Not one of the rewriter's owner modules: the facade serves every module-level
name of an owner, and this helper needs none of them served.
"""

from __future__ import annotations

import contextlib
import logging
import os
from pathlib import Path
from typing import Callable

from kiro_crew import platform_compat
from kiro_crew.atomic_write import anchored_parent, refuse_linked_parent
from kiro_crew.pinned_fs import pin_parent, supports_pinned_walk

# Logged under the rewriter's logger so an operator reading one pass reads one stream.
logger = logging.getLogger("kiro_crew.mcp_gateway.rewriter")

#: A name under the folder being pruned, for the link check on its parent chain.
_PRUNE_PROBE = "*.json"

#: Whether a folder can be pinned by descriptor and pruned relative to it
#: (``unlink(name, dir_fd=...)``); false on Windows, which prunes by path.
_PINNED_PRUNE = (
    hasattr(os, "O_DIRECTORY")
    and hasattr(os, "O_NOFOLLOW")
    and os.unlink in os.supports_dir_fd
    and os.listdir in os.supports_fd
)


def prune_stale_json(folder: Path, keep: Callable[[str], bool]) -> None:
    """Remove the ``*.json`` files in *folder* whose names *keep* does not accept.

    The folder and every folder above it (down from the data home) must pass
    :func:`refuse_linked_parent`; if one does not, nothing is listed or removed.
    Where ``unlink`` takes ``dir_fd`` and ``O_NOFOLLOW`` exists, the folder is
    then opened one ``O_NOFOLLOW`` component at a time and each file is removed
    relative to that descriptor. Otherwise (Windows) the files are removed by
    path. A missing folder or a failed removal is skipped; a folder that cannot be
    opened or listed is logged and skipped.
    """
    try:
        refuse_linked_parent(folder / _PRUNE_PROBE)
    except OSError as exc:
        if any(platform_compat.is_link_or_junction(p) for p in (folder, *folder.parents)):
            logger.warning("overlay folder %s is a link; skipped: %s", folder, exc)
        else:
            logger.warning("overlay folder %s could not be checked; skipped: %s", folder, exc)
        return
    # ``pin_parent`` also needs ``open(dir_fd=...)``, which the constant does not test.
    if not (_PINNED_PRUNE and supports_pinned_walk()):
        for stale in folder.glob("*.json"):
            if not keep(stale.name):
                with contextlib.suppress(OSError):
                    stale.unlink()
        return
    # One no-follow open per component, so a parent swapped for a link after the
    # check fails its own open. Below the data home the names stay lexical; outside
    # it only the parent is resolved (a link there is the operator's layout) and the
    # folder's own name is still opened without following.
    anchored = anchored_parent(folder)
    try:
        fd = pin_parent(
            anchored if anchored is not None else str(folder.parent.resolve() / folder.name),
            what="overlay folder",
            refusal=OSError,
        )
    except FileNotFoundError:
        return
    except (OSError, RuntimeError) as exc:  # RuntimeError: a link loop in resolve()
        logger.warning("overlay folder %s could not be opened; skipped: %s", folder, exc)
        return
    try:
        try:
            names = os.listdir(fd)
        except OSError as exc:
            logger.warning("overlay folder %s could not be listed; skipped: %s", folder, exc)
            return
        for name in names:
            if name.endswith(".json") and not keep(name):
                with contextlib.suppress(OSError):
                    os.unlink(name, dir_fd=fd)
    finally:
        os.close(fd)
