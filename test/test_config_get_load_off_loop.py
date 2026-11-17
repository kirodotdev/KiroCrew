"""``GET /api/config/kirocrew`` loads the config off the event loop.

``KiroCrewConfig.load()`` reads the file and validates it against the schema. The
GET branch ran it on the loop thread on every Settings page load; the PATCH branch
already ran the same call in a worker thread.
"""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import MagicMock

import pytest

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.handlers import core as core_mod


@pytest.mark.asyncio
async def test_get_loads_the_config_in_a_worker_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    from kiro_crew.dashboard.handlers import api_kirocrew_config

    cfg = KiroCrewConfig()
    seen: list[int] = []

    def _load(_cls, *_a, **_k):
        seen.append(threading.get_ident())
        return cfg

    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(_load))
    monkeypatch.setattr(core_mod, "_masked_config_dict", lambda _c: {})
    monkeypatch.setattr(core_mod, "_kas_in_use", lambda _c: asyncio.sleep(0, result=None))
    req = MagicMock()
    req.method = "GET"

    resp = await api_kirocrew_config(req)

    assert resp.status == 200
    assert seen, "the config was not loaded"
    assert threading.get_ident() not in seen, "the config was loaded on the event loop"
