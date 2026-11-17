"""Tests for the api_kirocrew_config PUT validator's max_subagents handling.

Stage 5 of dynamic-subagent-sizing: the validator must accept the 0 = auto
sentinel and bound explicit values by subagent_auto_max (not the old 5).
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web

from kiro_crew.dashboard.handlers.core import api_kirocrew_config


async def _put(agent_settings: dict, cfg_path) -> web.Response:
    request = MagicMock(spec=web.Request)
    request.method = "PUT"
    state = MagicMock()
    state.owner_id = ""
    request.app = {"state": state}
    claims = {"user": "local-app", "app": ""}
    request.get = lambda key, default=None: claims.get(key, default)
    request.__contains__.side_effect = lambda key: key in claims
    request.__getitem__.side_effect = lambda key: claims[key]

    async def _json():
        return {"agent": agent_settings}

    request.json = _json
    with patch("kiro_crew.config.loader.config_path", return_value=cfg_path):
        return await api_kirocrew_config(request)


def _written(cfg_path) -> dict:
    return json.loads(cfg_path.read_text(encoding="utf-8"))


@pytest.mark.asyncio
async def test_accepts_zero_auto_sentinel(tmp_path):
    cfg = tmp_path / "config.json"
    res = await _put({"max_subagents": 0}, cfg)
    assert res.status == 200
    assert _written(cfg)["agent"]["max_subagents"] == 0


@pytest.mark.asyncio
async def test_accepts_value_up_to_hard_cap(tmp_path):
    cfg = tmp_path / "config.json"
    # Default hard cap is the loader's subagent_auto_max default (32), so 12 is accepted.
    res = await _put({"max_subagents": 12}, cfg)
    assert res.status == 200
    assert _written(cfg)["agent"]["max_subagents"] == 12


@pytest.mark.asyncio
async def test_rejects_above_hard_cap(tmp_path):
    cfg = tmp_path / "config.json"
    res = await _put({"max_subagents": 99}, cfg)
    assert res.status == 400


@pytest.mark.asyncio
async def test_rejects_negative(tmp_path):
    cfg = tmp_path / "config.json"
    res = await _put({"max_subagents": -1}, cfg)
    assert res.status == 400


@pytest.mark.asyncio
async def test_same_request_cannot_widen_hard_cap(tmp_path):
    cfg = tmp_path / "config.json"
    # Security: the ceiling MUST come from persisted config only. A request that
    # tries to raise subagent_auto_max in the same payload must NOT widen the
    # bound — with no persisted value the default hard cap (32) applies, so 40 is
    # rejected (deny-by-default; prevents {auto_max:9999, max_subagents:9999}).
    res = await _put({"subagent_auto_max": 64, "max_subagents": 40}, cfg)
    assert res.status == 400


@pytest.mark.asyncio
async def test_default_config_accepts_the_cap_the_page_shows(tmp_path):
    cfg = tmp_path / "config.json"
    # With nothing persisted the page shows the loader's default cap (32) as the
    # input's max and sends it back on Save; a count under it must save.
    res = await _put({"subagent_max_turns": 100, "max_subagents": 20, "subagent_auto_max": 32}, cfg)
    assert res.status == 200
    assert _written(cfg)["agent"]["max_subagents"] == 20


@pytest.mark.asyncio
async def test_hard_cap_honors_persisted_config(tmp_path):
    cfg = tmp_path / "config.json"
    # An already-persisted higher ceiling DOES raise the bound.
    cfg.write_text(json.dumps({"agent": {"subagent_auto_max": 32}}), encoding="utf-8")
    res = await _put({"max_subagents": 30}, cfg)
    assert res.status == 200
    assert _written(cfg)["agent"]["max_subagents"] == 30


@pytest.mark.asyncio
async def test_subagent_max_turns_still_bounded(tmp_path):
    cfg = tmp_path / "config.json"
    res = await _put({"subagent_max_turns": 9999}, cfg)
    assert res.status == 400  # generic 1..SUBAGENT_MAX_TURNS_CEILING rule unchanged
