"""The start priority a dashboard request's own work runs at.

One owner for the question every dashboard claimer asks -- "is the person who owns
this dashboard waiting on this?" -- so the answer cannot differ per handler. The
rule it serves is :mod:`kiro_crew.start_priority`.

Its own module rather than a helper beside ``is_owner_dashboard_request``: that
handler's module-level surface is pinned name-for-name
(``test_source_providers_refactor_facade.py``), and a scheduling helper is not part
of what those callers, patches and star imports reach for.
"""

from __future__ import annotations

from aiohttp import web

from kiro_crew.start_priority import StartPriority, person_priority


def owner_start_priority(request: web.Request) -> StartPriority:
    """FOREGROUND when the dashboard owner made *request*, else BACKGROUND.

    An app token, an internal-secret caller and an unauthenticated request are all
    BACKGROUND: the person-only cold-start reserve is the one resource an app must
    not take from a person, even on a slot that app owns. Positive identity, from
    the dashboard's own owner gate -- never "not an app".
    """
    # Call-time import: the owner gate's module imports much of the dashboard.
    from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request

    return person_priority(is_owner_dashboard_request(request))
