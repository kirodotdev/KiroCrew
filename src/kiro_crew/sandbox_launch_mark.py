"""Whether the sandbox launcher marked this process, remembered past the entry point.

``cli.main`` drops ``KIROCREW_SANDBOX_ACTIVE`` before it does anything else, so a nested
``wrap_argv`` re-wraps instead of trusting an inherited marker. The cloud human-action
guard (``cloud.aws._in_agent_session``) still has to know, later in the same process,
whether it was launched inside a sandbox, so ``cli.main`` calls :func:`note` just before
it drops the marker.

The answer only adds refusals, never removes one: a process that sets the marker makes
the guard refuse it, and one that clears it is still judged by the guard's other signals.
Stdlib only.
"""

from __future__ import annotations

import os
from typing import Mapping, Optional

#: Equal to ``sandbox_tiers._IN_SANDBOX_MARKER``; spelled here so this module imports no
#: sandbox code. ``test_cloud_aws`` pins the two equal.
SANDBOX_MARKER = "KIROCREW_SANDBOX_ACTIVE"

_launched_in_sandbox = False


def note(environ: Optional[Mapping[str, str]] = None) -> None:
    """Remember that the launcher marked this process, if it did. Never forgets."""
    global _launched_in_sandbox
    if (os.environ if environ is None else environ).get(SANDBOX_MARKER):
        _launched_in_sandbox = True


def launched_in_sandbox() -> bool:
    """Whether the marker was set at an entry point or is set now."""
    return _launched_in_sandbox or bool(os.environ.get(SANDBOX_MARKER))
