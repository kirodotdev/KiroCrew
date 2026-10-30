"""The Shared Crew release gate: ``KIROCREW_SHARED_CREW_PREVIEW=1``.

Until the owner ships Shared Crew, nothing in it may be switched on from
config by accident. With the variable unset (the default) every
``dashboard.shared_crew`` switch reads as its default-off value whatever
config.json says, so the gateway behaves as it did before Shared Crew. Set
to ``1`` in the gateway's environment, config decides as before. An
environment variable only: no config field, Settings row or change card can
turn it on.
"""

from __future__ import annotations

import os

#: The gateway environment variable that opts this host into the preview.
PREVIEW_ENV = "KIROCREW_SHARED_CREW_PREVIEW"


def preview_enabled() -> bool:
    """Whether this process runs the Shared Crew preview. Exactly ``1`` turns it on."""
    return os.environ.get(PREVIEW_ENV, "") == "1"


__all__ = ["PREVIEW_ENV", "preview_enabled"]
