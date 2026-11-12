"""``GET /api/mcp`` and ``GET /api/mcp/active`` read the server list off the event loop.

``list_servers()`` reads the agent config and every scope's ``mcp.json``. The two
routes called it on the loop thread, so a slow home mount stalled every other
request; the cached-probe route already ran it in a worker thread.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

import kiro_crew.mcp_discovery as disc
from kiro_crew.dashboard.handlers import mcp as mcp_mod


class _State:
    def __init__(self) -> None:
        self._background_tasks: set = set()
        self.sessions = SimpleNamespace(final_drain_started=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["api_mcp_servers", "api_mcp_active"])
async def test_list_servers_runs_in_a_worker_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    threads: list[int] = []

    def _spy(*_a: Any, **_k: Any) -> list:
        threads.append(threading.get_ident())
        return []

    g = tmp_path / "mcp.json"
    g.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(mcp_mod, "_GLOBAL_MCP_JSON", g)
    monkeypatch.setattr(mcp_mod, "_kirocrew_mcp_json", lambda: tmp_path / "absent.json")
    monkeypatch.setattr(disc, "list_servers", _spy)
    monkeypatch.setattr(mcp_mod, "_bg_mcp_probe", AsyncMock(return_value=None))
    monkeypatch.setattr(mcp_mod, "_mcp_probe_cache", [])
    monkeypatch.setattr(mcp_mod, "_mcp_probe_ts", time.time())
    monkeypatch.setattr(mcp_mod, "_mcp_probe_in_progress", False)
    req = MagicMock()
    req.app = {"state": _State()}
    req.query = {}

    resp = await getattr(mcp_mod, route)(req)

    assert resp.status == 200
    assert threads, "list_servers was not called"
    assert threading.get_ident() not in threads, "list_servers ran on the event loop"
