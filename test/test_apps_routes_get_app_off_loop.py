"""The update and uninstall routes read the installed row off the event loop.

``get_app`` builds the row's ``agentToolsGrant``, which reads the app's provenance
(``app_agent_tools.is_third_party``): a scan of every built-in manifest.
"""

from __future__ import annotations

import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from dashboard_owner_helpers import owner_claims

import kiro_crew.apps.routes as routes_mod


def _request() -> MagicMock:
    request = MagicMock()
    request.match_info = {"name": "demo"}
    state = MagicMock()
    state.owner_id = ""
    request.app = {"state": state}
    owner_claims(request)
    return request


@pytest.mark.asyncio
@pytest.mark.parametrize("handler", ["handle_update_app", "handle_uninstall_app"])
async def test_route_reads_the_installed_row_off_the_loop_thread(handler: str) -> None:
    loop_thread = threading.current_thread()
    threads: list[threading.Thread] = []

    def _recording_get_app(name: str) -> None:
        threads.append(threading.current_thread())
        return None

    with (
        patch.object(routes_mod, "get_app", _recording_get_app),
        patch("kiro_crew.bot_chat_app_tools.after_app_change", new_callable=AsyncMock),
        patch(
            "kiro_crew.bot_chat_ending_fence.stop_tracked_bot_chats",
            new_callable=AsyncMock,
            return_value=[],
        ),
    ):
        resp = await getattr(routes_mod, handler)(_request())

    assert resp.status == 404, resp.status
    assert threads, "get_app was not called"
    assert loop_thread not in threads, f"{handler} ran get_app on the event loop thread"


@pytest.mark.asyncio
async def test_open_route_asks_execution_admission_off_the_loop_thread() -> None:
    """A granted app's admission reads its installed row, so it is asked off the loop."""
    loop_thread = threading.current_thread()
    threads: list[threading.Thread] = []

    def _recording_denied(name: str, **_kwargs: object) -> str:
        threads.append(threading.current_thread())
        return "denied for the test"

    info = {"enabled": True, "manifest": {"openCommand": "true"}}
    with (
        patch(
            "kiro_crew.dashboard.handlers._shared.require_owner_dashboard_request",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch.object(routes_mod, "get_app", lambda name: info),
        patch.object(routes_mod, "app_execution_denied", _recording_denied),
    ):
        resp = await routes_mod.handle_open_app(_request())

    assert resp.status == 403, resp.status
    assert threads, "app_execution_denied was not called"
    assert loop_thread not in threads, "handle_open_app ran app_execution_denied on the loop"
