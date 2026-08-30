"""The one import surface between application code and an agent backend.

Nothing lives here yet. This package is declared first, on purpose: the boundary
it names is enforced from the moment it exists, by
``scripts/check_agent_sdk_boundary.py`` and ``test/test_agent_sdk_boundary.py``,
so the leak it is meant to drain cannot grow while the contents are still being
designed.

Layering, once populated::

    consumers        dashboard/  slack/  discord/  telegram/  messaging/
                     session.py  subagent.py  apps/  cli_*.py  workflows/
                            |
                            |  may import ONLY kiro_crew.agent_sdk
                            v
                     kiro_crew.agent_sdk          domain types, role protocols,
                                                  capabilities, supervisor
                            |
                            |  resolves drivers through a registry
                            v
                     kiro_crew.agent_sdk.drivers.acp
                                                  the ONLY module permitted to
                                                  import kiro_crew.acp
                            v
                     kiro_crew.acp   (private)    wire, dialects, adapters,
                                                  session handles, worker pool

If the gate goes red you introduced a boundary violation: fix the import
direction rather than relaxing the rule, and never add or raise a line in
``.github/agent-sdk-boundary-baseline.txt`` to make it green.

Design of record, including what each later phase moves in here:
``docs/request-for-change/rfc-crew-agent-sdk-boundary.md``.
"""

from __future__ import annotations

__all__: list[str] = []
