"""The Kiro relay viewer: open a remote Crew through the relay, from this machine.

The viewer leg of Remote Control, the other side of :mod:`kiro_crew.relay_client`.
:mod:`~kiro_crew.relay_viewer.cps` lists the Crews a person can open and mints
the access token that opens one. Nothing is wired in yet: no route, setting or
window calls this package.
"""

from kiro_crew.relay_viewer.cps import (
    CpsRefused,
    CrewAccessToken,
    CrewEndpoint,
    create_crew_access_token,
    list_crew_endpoints,
)

__all__ = [
    "CpsRefused",
    "CrewAccessToken",
    "CrewEndpoint",
    "create_crew_access_token",
    "list_crew_endpoints",
]
