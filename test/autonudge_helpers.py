"""Fixtures and builders for the modules split out of ``test_autonudge.py``.

``_enable`` is autouse; each of those modules imports it, so every test keeps the autouse set
it had before the split.
"""

from __future__ import annotations

import pytest

from kiro_crew.autonudge import AutoNudgeService
from kiro_crew.monitoring.models import MonitorState


def _owner_dashboard_identity():
    """The owner's dashboard claims, for the routes the owner gate covers.

    ``app == ""`` with the owner's subject: ``state.owner_id`` when one is
    configured, else the signed local bootstrap subject.
    """
    from aiohttp import web

    @web.middleware
    async def middleware(request, handler):
        state = request.app.get("state")
        request["user"] = str(getattr(state, "owner_id", "") or "") or "local-app"
        request["app"] = ""
        return await handler(request)

    return middleware


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- no test in the test_autonudge*.py modules calls monkeypatch.undo()
def _enable(monkeypatch):
    monkeypatch.setenv("KIROCREW_AUTONUDGE", "1")


@pytest.fixture(name="svc")
def svc_fixture(tmp_path):
    return AutoNudgeService(base_dir=tmp_path)


def _structured_monitor(**changes: object) -> MonitorState:
    values: dict[str, object] = {
        "kind": "github_pull_request",
        "target": "owner/repo#123",
        "objective": "review_ready",
        "created_ts": 1_000.0,
    }
    values.update(changes)
    return MonitorState(**values)
