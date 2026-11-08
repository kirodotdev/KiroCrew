"""Newest-first order of local snapshot bundles.

``kirocrew snapshot`` names each bundle ``kirocrew-snapshot-<UTC stamp>.tar.gz``.
The stamp is when the bundle was made; a file's mtime is when it last landed on
this disk, which a copy, a restore from off-host storage or a touch moves. So
``--list`` and ``--keep`` order by the stamp, and use the mtime only for a name
that carries none.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path


def newest_first(paths: list[Path]) -> list[Path]:
    """*paths* ordered newest bundle first: by the name's stamp, else the mtime.

    The mtime also breaks a tie between two bundles stamped in the same second.
    """

    def age(path: Path) -> tuple[float, float]:
        mtime = path.stat().st_mtime
        # The stamp ``snapshot_main`` writes (``%Y%m%dT%H%M%SZ``), right after the prefix.
        match = re.match(r"kirocrew-snapshot-(\d{8}T\d{6}Z)", path.name)
        if match is None:
            return (mtime, mtime)
        try:
            made = datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ")
        except ValueError:
            return (mtime, mtime)
        return (made.replace(tzinfo=timezone.utc).timestamp(), mtime)

    return sorted(paths, key=age, reverse=True)
