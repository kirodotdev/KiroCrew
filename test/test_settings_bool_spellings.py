"""A settings PUT that spells a switch as a string saves the switch it names.

``bool("false")`` is ``True``, so ``PUT /api/stt/config {"enabled": "false"}``
saved speech-to-text ON and ``PUT /api/memory/settings {"migrated": "false"}``
marked memory migrated. A spelled ``"false"``/``"0"``/``"no"``/``"off"`` (any
case) saves ``False`` and its ``true`` spellings save ``True``. Every other value
saves as it always has, so nothing that saves today is refused.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from test_config_savers_locked_write import _seed
from test_handlers_memory_coverage import _MOD, _body, _cfg, _make_request, _make_state

from kiro_crew.dashboard.handlers import memory as mem_mod

_CASES = [
    ("false", False),
    ("False", False),
    ("0", False),
    ("off", False),
    ("no", False),
    ("true", True),
    (True, True),
    (False, False),
    (1, True),
    (0, False),
    ("yes please", True),  # not a spelling: saved by truthiness, as before
]


@pytest.mark.parametrize(("sent", "saved"), _CASES)
def test_the_stt_switch_saves_what_it_spells(tmp_path: Path, monkeypatch, sent, saved) -> None:
    import kiro_crew.dashboard.handlers.core as core

    cfg = _seed(monkeypatch, tmp_path, {"stt": {"enabled": not saved}})
    monkeypatch.setattr(core, "config_path", lambda: cfg)
    monkeypatch.setattr(core, "_stt_prereq_commands", lambda provider: {})
    monkeypatch.setattr(core, "availability_detail", lambda stt: core.stt.Availability(False))
    req = MagicMock(spec=web.Request)
    req.method = "PUT"
    req.json = AsyncMock(return_value={"enabled": sent})

    resp = asyncio.run(core.api_stt_config(req))

    assert resp.status == 200
    assert json.loads(cfg.read_text(encoding="utf-8"))["stt"]["enabled"] is saved


@pytest.mark.asyncio
@pytest.mark.parametrize(("sent", "saved"), _CASES)
async def test_the_memory_migrated_flag_saves_what_it_spells(tmp_path: Path, sent, saved) -> None:
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"memory": {"migrated": not saved}}), encoding="utf-8")
    state = _make_state()
    state.consolidator = None
    req = _make_request(state, method="PUT", json_body={"migrated": sent})
    with (
        patch(f"{_MOD}.KiroCrewConfig.load", return_value=_cfg()),
        patch(f"{_MOD}.config_path", return_value=cfg_path),
    ):
        resp = await mem_mod.api_memory_settings(req)

    assert _body(resp) == {"ok": True}
    assert json.loads(cfg_path.read_text(encoding="utf-8"))["memory"]["migrated"] is saved
