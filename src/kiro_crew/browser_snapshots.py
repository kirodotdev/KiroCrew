"""Where the agent's browser screenshots land.

One fixed directory under the data home, so the ``browser`` tool's
``screenshot`` op and the channel reply path (:mod:`kiro_crew.messaging.outbound_files`)
agree on the one place a screenshot the agent just took can be. It is never
inside a working directory by construction, and deriving it from
``config_dir()`` keeps an isolated ``KIROCREW_HOME`` (a pod, a test) isolated
here too.
"""

from __future__ import annotations

from pathlib import Path

from kiro_crew.config.paths import config_dir

#: Leaf name under the data home. Kept literal by every reader that pins a
#: read to it, so a link planted at this name resolves elsewhere and fails
#: the pin instead of moving the boundary.
SNAPSHOT_DIR_NAME = "browser-snapshots"


def snapshot_dir() -> Path:
    """The screenshot directory for this data home (not created here)."""
    return config_dir() / SNAPSHOT_DIR_NAME
