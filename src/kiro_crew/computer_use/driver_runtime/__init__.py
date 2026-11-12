"""The upstream desktop driver runtime: pin, install, environment fence, supervisor.

Kiro Crew can drive the desktop through the open-source ``cua-driver`` (MIT, by
Cua AI, Inc.), the same way Browser Use runs on the pinned Browser Harness. No
upstream code ships in this package: when the owner turns the driver on, the
gateway downloads the official release archive for this OS and architecture,
checks its size and sha256 against the table in :mod:`.pin`, extracts it into a
private, read-only directory under the data home, and runs the binary as one
supervised stdio child (:mod:`.supervisor`).

The agent never talks to the driver. The model sees only the ``computer_*``
tools; :class:`~.backend.CuaBackend` maps each verb onto a closed allowlist of
upstream tool names, so the driver's own tools that reach the network, install
software or run code (an update check, an extension installer, a script runner,
a browser bridge, a recorder) cannot be called by anything.

Never run here: upstream's install scripts, ``update``, ``connect``,
``mcp-config``, ``skills``, ``autostart`` or ``telemetry install-event``.
"""

from __future__ import annotations

from kiro_crew.computer_use.driver_runtime.pin import DRIVER_VERSION

__all__ = ["DRIVER_VERSION"]
