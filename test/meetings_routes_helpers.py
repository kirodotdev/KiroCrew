"""Shared helpers for the ``test_meetings_routes*.py`` route tests.

The route base path and the two calls that start a meeting over the HTTP API.
The fixtures and the fake session manager are in ``meetings_helpers``.
"""

from __future__ import annotations

from kiro_crew.apps.builtins.meetings.backend import constants as k
from kiro_crew.apps.builtins.meetings.backend.routes import _common

BASE = k.API_BASE


async def _start(client, meeting_id: str = "standup", **body) -> dict:
    await client.post(f"{BASE}/meetings/{meeting_id}/init", json={"title": "Standup"})
    resp = await client.post(f"{BASE}/meetings/{meeting_id}/start", json=body)
    assert resp.status == 200, await resp.text()
    return await resp.json()


async def _start_and_get_session(client, meeting_id: str = "standup", **body):
    """Start *meeting_id* and hand back its installed live session."""
    await _start(client, meeting_id, **body)
    session = _common.ACTIVE.get(meeting_id)
    assert session is not None, f"{meeting_id} did not install a live session"
    return session
