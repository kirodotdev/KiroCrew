"""Shared harness for the design-critique backend route tests (``test_backend_routes*.py``).

The owner request the handlers read their JSON body from, and the bounded poll that waits
for a detached job to finish. A helper only one part uses stays in that part.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from kiro_crew.apps.builtins.design_critique.backend import routes

_OWNER = "owner-user"


class _Req(dict):
    """Minimal stand-in for an owner's dashboard request.

    Carries the signed claims the owner gate reads (``user``, an empty ``app``)
    and the ``state.owner_id`` it compares them to, plus the one method the
    handler awaits.
    """

    def __init__(self, payload: object) -> None:
        super().__init__(user=_OWNER, app="")
        self.app = {"state": SimpleNamespace(owner_id=_OWNER)}
        self._payload = payload

    async def json(self) -> object:
        return self._payload


async def _drain(job_id: str) -> dict[str, Any] | None:
    # Poll the in-memory record until the detached task finishes (or give up).
    for _ in range(400):
        rec = routes._get_job(job_id)
        if rec and rec["status"] != "running":
            return rec
        # flake-ok: 5 ms interval of a bounded 2 s poll that re-reads the job record
        await asyncio.sleep(0.005)
    return routes._get_job(job_id)
