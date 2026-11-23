"""The session-directive park/claim store, at its dashboard import path.

The store lives in :mod:`kiro_crew.directive_queue`, a neutral module, because
the messaging layer claims from it too and may not import the dashboard. This
path names the SAME module object rather than a copy, so a caller that patches
or resets the store here (``directive_queue._pending``, ``Store`` caps) changes
the one store every consumer reads.
"""

import sys
from typing import TYPE_CHECKING

from kiro_crew import directive_queue as _store

if TYPE_CHECKING:
    # What the alias below serves at runtime, spelled for type checkers.
    from kiro_crew.directive_queue import (  # noqa: F401
        DIRECTIVES,
        MAX_AGE_SECS,
        MAX_PER_SESSION,
        MAX_SESSIONS,
        Store,
        claim,
        depth,
        discard,
        publish,
        reset,
        unclaimed_digest_markers,
    )

sys.modules[__name__] = _store
