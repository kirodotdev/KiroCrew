"""The gateway stub toggle reads ``config.local.json`` off the event loop.

``api_mcp_gateway_set_stub`` read and parsed the local overlay on the loop thread
on every click; the sibling enable toggle already ran the same read in a worker
thread.
"""

from __future__ import annotations

import threading

import pytest
from mcp_launch_approval_helpers import _stub_request  # noqa: F401
from mcp_launch_approval_helpers import toggle_home_fixture  # noqa: F401


@pytest.mark.asyncio
async def test_set_stub_reads_the_overlay_in_a_worker_thread(toggle_home, monkeypatch) -> None:
    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    seen: list[int] = []

    def _overlay() -> dict:
        seen.append(threading.get_ident())
        return {}

    monkeypatch.setattr(mcp_mod, "_local_overlay_section", _overlay)

    resp = await mcp_mod.api_mcp_gateway_set_stub(_stub_request({"name": "srv", "stub": False}))

    assert resp.status == 200
    assert seen, "the overlay was not read"
    assert threading.get_ident() not in seen, "the overlay was read on the event loop"
