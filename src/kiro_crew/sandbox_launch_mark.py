"""Whether the sandbox launcher marked this process, remembered past the entry point.

``cli.main`` and ``mcp_entry.slim_server`` drop ``KIROCREW_SANDBOX_ACTIVE`` before they
do anything else, so a nested ``wrap_argv`` re-wraps instead of trusting an inherited
marker. The governance tier still has to know, later in the same process, whether it was
launched inside a sandbox: an enterprise build's sandboxed child takes the ceiling from
the policy cache the gateway wrote, read-only, instead of fetching one
(``policy_distribution_source.cache_only``). Each entry point calls :func:`note` just
before it drops the marker.

The answer selects WHERE the ceiling comes from, never whether one applies. A sandboxed
child that clears the marker would try to fetch, which the sandbox denies, and fail
closed. An unsandboxed process that sets it reads only the cache, which it could write
anyway. Stdlib only, so the slim MCP entry stays light.
"""

from __future__ import annotations

import os
from typing import Mapping, Optional

#: Equal to ``sandbox_tiers._IN_SANDBOX_MARKER``; spelled here so this module imports no
#: sandbox code. ``test_policy_cache_sandbox_view`` pins the two equal.
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
