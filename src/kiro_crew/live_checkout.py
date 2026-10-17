"""Warn when the running gateway's code is a git work tree someone can rebase.

A gateway launched straight from a checkout imports its code from that
checkout's files. Pooled MCP backends it spawns later import the same files
again, so a ``git rebase`` / ``checkout`` / ``pull`` in that directory while the
gateway is up hands them a mix of old and new modules. The failure is far from
its cause -- in one outage every tool-policy read failed for hours -- so the boot
log names the risk up front, where an operator reading it will look first.
"""

from __future__ import annotations

import logging
from pathlib import Path

from kiro_crew._bootstrap import _source_checkout_root

logger = logging.getLogger(__name__)


def warn_if_running_from_git_checkout(source_root: Path | None = None) -> Path | None:
    """Log one WARNING when this install's source checkout is a git work tree.

    ``source_root`` defaults to :func:`kiro_crew._bootstrap._source_checkout_root`,
    the one answer to "is this a source install, and where". ``.git`` is a
    directory in a main clone and a file in a linked worktree; both count.
    Returns the work tree (or ``None``) so tests can see what was found. Never
    raises: a step that only warns must not stop boot.
    """
    try:
        root = source_root if source_root is not None else _source_checkout_root()
        if root is None or not (root / ".git").exists():
            return None
    except Exception:
        logger.debug("live-checkout check skipped", exc_info=True)
        return None
    logger.warning(
        "Gateway code is running from git work tree %s. Do not rebase, checkout, "
        "switch, merge, pull or reset in it while the gateway runs: its MCP "
        "backends would load mixed old and new code. Make a worktree "
        "(git worktree add) for development instead.",
        root,
    )
    return root
