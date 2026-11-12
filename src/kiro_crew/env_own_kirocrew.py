"""The crew-home directory that puts this install's ``kirocrew`` ahead of ``~/.local/bin``.

:func:`kiro_crew.env.augmented_path` lists :func:`own_kirocrew_dirs` first on
every agent PATH. The gateway refreshes the one link inside it on every start
(``agent.refresh_own_kirocrew_link``) and the sandbox seals the directory
read-only (``sandbox_leaves``, ``sandbox_precreate``, ``security.paths_lists``).
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable

from kiro_crew import platform_compat
from kiro_crew.config.paths import peek_data_home

logger = logging.getLogger(__name__)

#: The crew-home directory holding ONE entry, a ``kirocrew`` link to the install
#: this gateway runs.
OWN_KIROCREW_DIR = "kirocrew-bin"


def own_kirocrew_dirs(validate: Callable[[str], str | None]) -> list[str]:
    """The directory whose only entry is this install's ``kirocrew``, ahead of ``~/.local/bin``.

    An agent shelling out to ``kirocrew`` must reach its own gateway's CLI. A
    stale launcher in ``~/.local/bin`` would otherwise win, and an old CLI saves
    ``config.json`` with a schema lacking member identity, which bricks every
    crewmate. The interpreter dir cannot move forward instead: it also holds
    ``python``/``pip`` (see :func:`kiro_crew.env.augmented_path`).

    *validate* is ``env._validated_bin_dir``. POSIX only: on Windows the
    launcher is a ``.exe``/``.cmd`` pair beside the interpreter. Nothing is
    created here; a missing or dangling link is skipped by every PATH lookup.
    """
    if platform_compat.IS_WINDOWS:
        return []
    try:
        root = peek_data_home() / OWN_KIROCREW_DIR
    except Exception:
        logger.debug("could not resolve the own-kirocrew directory", exc_info=True)
        return []
    value = validate(str(root))
    # A link at the NAME would put whatever directory it points at ahead of
    # ``~/.local/bin``; only the real, sealed directory is listed.
    if value is None or os.path.islink(value):
        return []
    return [value]
