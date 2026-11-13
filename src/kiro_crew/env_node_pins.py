"""The Node bin dirs :func:`kiro_crew.env.augmented_path` ranks ahead of nvm's.

``augmented_path`` lists every per-version manager dir, newest first, and none
is checked to load. On Amazon Linux 2 (glibc 2.26) the official Node 18+ builds
need glibc 2.28, so nvm's newest ``node`` fails and an ``npx`` MCP server, whose
``#!/usr/bin/env node`` resolves against the same PATH, never starts. Two dirs
name a Node chosen for this host instead:

* :func:`override_bin_dirs`, the operator's ``KIROCREW_NODE_BIN_DIR``, which
  ``augmented_path`` lists right after this install's own ``kirocrew`` dir and
  never anchors on an inherited copy (:func:`without_dirs`);
* :func:`data_home_tree_dirs`, the glibc-2.17 tree ``ensure-node.sh`` unpacks
  into the data home. It is an unsealed data-home dir, so ``augmented_path``
  ranks it only ahead of the per-version manager dirs, never ahead of
  ``~/.local/bin`` or the toolbox.

The ``node-bin-dir`` marker ``ensure-node.sh`` writes is not read here: it is a
writable data-home file and ``augmented_path`` also feeds the trusted
agent-runtime resolvers. Where the marker matters (AL2) it names the glibc-2.17
tree, which :func:`data_home_tree_dirs` already covers.

The two dir helpers take ``env._validated_bin_dir`` and ``env._has_node`` as arguments
so this module does not import :mod:`kiro_crew.env`.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Iterable
from pathlib import Path

from kiro_crew.config.paths import peek_data_home

logger = logging.getLogger(__name__)


def _with_node(d: str, has_node: Callable[[Path], bool]) -> bool:
    try:
        return has_node(Path(d))
    except OSError:
        return False


def override_bin_dirs(
    env_var: str,
    validate: Callable[[str], str | None],
    has_node: Callable[[Path], bool],
) -> list[str]:
    """The *env_var* dir when it is one absolute dir holding ``node``, else ``[]``.

    Read from ``os.environ`` on every call rather than through
    ``env.node_bin_dirs``'s process-lifetime cache, so a value the data home's
    ``.env`` seeds after boot still counts.
    """
    value = validate(os.environ.get(env_var, ""))
    if value is None:
        return []
    value = os.path.normpath(value)
    return [value] if _with_node(value, has_node) else []


def without_dirs(entries: list[str], dirs: list[str]) -> list[str]:
    """*entries* less every spelling of *dirs*, empty entries kept.

    ``augmented_path`` treats a well-known dir the inherited PATH already lists
    as an anchor that keeps the user's position, and an extra ranked after it
    follows it there. The override is ranked second, so an inherited copy behind
    nvm's bin would keep nvm's ``node`` first and pull ``~/.local/bin`` and the
    toolbox in behind both. Dropping the copy leaves the override nothing to
    anchor on, so it is always prepended.
    """
    if not dirs:
        return entries
    keys = {os.path.normcase(os.path.normpath(d)) for d in dirs}
    return [p for p in entries if not p or os.path.normcase(os.path.normpath(p)) not in keys]


def data_home_tree_dirs(
    rel_dirs: Iterable[str],
    validate: Callable[[str], str | None],
    has_node: Callable[[Path], bool],
) -> list[str]:
    """Each of *rel_dirs* under the data home that holds ``node``.

    ``peek_data_home`` creates nothing, which matters because this runs on the
    event-loop path of the MCP probe.
    """
    try:
        root = peek_data_home()
    except (OSError, RuntimeError, ValueError):
        logger.debug("could not resolve the data home for its Node tree", exc_info=True)
        return []
    out: list[str] = []
    for rel in rel_dirs:
        value = validate(os.path.normpath(str(root / rel)))
        if value is not None and _with_node(value, has_node):
            out.append(value)
    return out
