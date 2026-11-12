"""Shared Crew has one way to create an invite.

A second route or a second caller of the Tailscale create call would be a
parallel invite flow with its own gates (first-share acknowledgment, owner
check, audit), so the route table and the call sites are pinned here.
"""

from __future__ import annotations

import re
from pathlib import Path

from aiohttp import web

import kiro_crew
from kiro_crew.shared_crew import routes_invites

#: Every call of a ``create_invites`` function or method in the package.
_CALL = re.compile(r"\bcreate_invites\(")
#: ``def`` lines are definitions, not calls.
_DEF = re.compile(r"^\s*(?:async\s+)?def\s")


def _registered() -> list[tuple[str, str]]:
    app = web.Application()
    routes_invites.register(app)
    return [(r.method, r.resource.canonical) for r in app.router.routes() if r.resource]


def test_one_route_creates_invites() -> None:
    creating = [
        (method, path)
        for method, path in _registered()
        if method == "POST"
        and path.startswith(routes_invites.BASE)
        and "invites" in path
        and not path.endswith("/resend")
    ]
    assert creating == [("POST", routes_invites.STATUS_PATH)]


def test_create_invites_has_one_caller_per_layer() -> None:
    root = Path(kiro_crew.__file__).parent
    callers: dict[str, int] = {}
    for path in root.rglob("*.py"):
        count = sum(
            1
            for line in path.read_text(encoding="utf-8").splitlines()
            if _CALL.search(line) and not _DEF.match(line)
        )
        if count:
            callers[path.relative_to(root).as_posix()] = count
    # The route calls the service, and the service calls the Tailscale client.
    assert callers == {
        "shared_crew/routes_invites.py": 1,
        "shared_crew/tailscale_invites.py": 1,
    }
