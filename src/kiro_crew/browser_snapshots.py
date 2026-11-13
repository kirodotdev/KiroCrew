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


#: Retention: screenshots are throwaway, so keep a day's worth and at most 200,
#: and never touch one younger than the grace window (the agent may be about to
#: read or send the path it was just given).
MAX_AGE_S = 24 * 60 * 60
MAX_FILES = 200
GRACE_S = 5 * 60


def snapshot_dir() -> Path:
    """The screenshot directory for this data home (not created here)."""
    return config_dir() / SNAPSHOT_DIR_NAME


def prune(directory: Path, now: float) -> None:
    """Drop screenshots past the age or count bound. Best effort, never raises."""
    try:
        shots = sorted(
            ((p.stat().st_mtime, p) for p in directory.glob("shot-*.png") if p.is_file()),
            reverse=True,
        )
    except OSError:
        return
    for index, (mtime, path) in enumerate(shots):
        age = now - mtime
        if age < GRACE_S or (age < MAX_AGE_S and index < MAX_FILES):
            continue
        try:
            path.unlink()
        except OSError:
            pass
