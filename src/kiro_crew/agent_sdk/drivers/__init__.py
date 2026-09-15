"""Backend drivers -- the only layer permitted to import ``kiro_crew.acp``.

One module per harness family. A driver's whole job is to answer the SDK's
questions in plain Python data -- bools, strings, tuples -- so that neither an
ACP type nor an ACP import reaches the layer above it. That is what makes the
boundary checkable: ``scripts/check_agent_sdk_boundary.py`` exempts this tree by
prefix, so an ACP import anywhere else is a violation with no legal remedy but
routing it through here.

``acp`` is the first driver, and today the only one. What each later phase moves
in beside it is recorded in
``docs/request-for-change/rfc-crew-agent-sdk-boundary.md`` §5.5.
"""

from __future__ import annotations

__all__: list[str] = []
