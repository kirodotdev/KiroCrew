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

    An agent that shells out to ``kirocrew`` must reach the CLI of the gateway
    it runs under. Without this, the lookup walks ``~/.local/bin`` first, and a
    launcher left there by an older install wins: an old CLI then loads and
    saves ``config.json`` with a schema that predates member identity and
    strips it, which bricks every crewmate. The interpreter's own
    directory cannot be moved forward instead, because it also holds
    ``python``/``pip`` (see :func:`kiro_crew.env.augmented_path`), so a
    directory carrying only the one name is put ahead of ``~/.local/bin``.

    *validate* is the bin-dir check every other PATH source goes through
    (``env._validated_bin_dir``).

    POSIX only: on Windows the launcher is a ``.exe``/``.cmd`` pair beside the
    interpreter, and no link is written. Nothing is created here; the entry is
    listed whether or not the gateway has written the link yet, and a missing or
    dangling link is skipped by every PATH lookup.
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
