"""Whether the install THIS process was imported from still exists.

An update installs the next version beside the running one and later prunes
the old directory, while long-lived processes started from it keep running.
Everything they already imported keeps working; everything they import LATER
fails with ``ModuleNotFoundError: No module named 'kiro_crew.<x>'`` -- the
package object is still in ``sys.modules`` but the file behind the submodule is
gone. A process in that state cannot heal itself: every lazily imported verb
stays broken until it is replaced.

Stdlib-only and imported at boot by the processes that consult it, so the
check itself can never be the import that fails after a prune.
"""

from __future__ import annotations

import os
from pathlib import Path

#: Set by the MCP gateway pool on every backend it spawns. Only there does an
#: exit lead to a respawn: the pool records the death and relaunches on the
#: next call. A server kiro-cli launched directly (the default, pool-less
#: topology) has no respawner, so exiting would take its tools away for the
#: rest of the session. Not ``KIROCREW_SPAWNED``: that marker is also set on
#: agent runtimes and so is inherited by the servers they launch directly.
#: A compile-time constant, so it cannot split or collapse pool identity.
POOLED_BACKEND_ENV = "KIROCREW_MCP_POOLED_BACKEND"
POOLED_BACKEND_VALUE = "1"

#: Package directory of the running process, fixed at import time. Resolved so
#: a symlinked launch path (a stable link repointed at the next version) is
#: judged by the tree the modules were actually read from.
_PACKAGE_ROOT = Path(__file__).resolve().parent

#: Process exit status for "my install is gone, replace me". ``EX_TEMPFAIL``,
#: the same status the gateway's stale-asset watchdog exits with, so a
#: restart-on-failure supervisor relaunches it.
INSTALL_PRUNED_EXIT_CODE = 75


def install_pruned(package_root: Path | None = None) -> bool:
    """True when the running package's ``__init__.py`` has disappeared.

    One ``stat``. ``__init__.py`` rather than the directory: a prune that
    leaves an empty directory node behind is still a prune, and a source tree
    mid-``git checkout`` never deletes the package's own ``__init__.py``.
    """
    root = package_root if package_root is not None else _PACKAGE_ROOT
    return not (root / "__init__.py").is_file()


def respawned_by_pool() -> bool:
    """True when this process is a pooled backend, so exiting gets it replaced."""
    return os.environ.get(POOLED_BACKEND_ENV) == POOLED_BACKEND_VALUE
