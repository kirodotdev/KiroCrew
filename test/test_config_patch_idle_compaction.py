"""PATCH /api/config/kirocrew for the two idle compaction keys Settings > Chat writes.

The claims: ``session.idle_compaction`` stores a real boolean and refuses
anything else; ``session.idle_compaction_min_context_pct`` stores a value inside
40-90 as sent and CLAMPS one outside it to the value a load of it produces, with a
``clamp_notice`` naming what was saved; a value that is not a finite number is
refused and leaves the file as it was.
"""

from __future__ import annotations

import json
import unittest.mock
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.config.sections import (
    IDLE_COMPACTION_MIN_CONTEXT_PCT_MAX,
    IDLE_COMPACTION_MIN_CONTEXT_PCT_MIN,
)

_SEED = {"agent": {"approval_mode": "auto"}, "session": {"autocompact_pct": 70.0}}
_MIN_PATH = "session.idle_compaction_min_context_pct"


@web.middleware
async def _owner_identity(request, handler):
    request["user"] = "local-app"
    request["app"] = ""
    state = request.app.get("state")
    if state is not None:
        state.owner_id = ""
    return await handler(request)


def _make_app() -> web.Application:
    from kiro_crew.dashboard.handlers import api_kirocrew_config_patch

    app = web.Application(middlewares=[_owner_identity])
    app["state"] = MagicMock()
    app.router.add_patch("/api/config/kirocrew", api_kirocrew_config_patch)
    return app


@pytest.fixture
def tmp_config(tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(_SEED), encoding="utf-8")
    with patch("kiro_crew.config.loader.config_path", return_value=cfg_path):
        yield cfg_path


async def _patch(path: str, value: object) -> tuple[int, dict]:
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.patch("/api/config/kirocrew", json={"path": path, "value": value})
        return resp.status, await resp.json()


def _session(cfg_path: Path) -> dict:
    return json.loads(cfg_path.read_text(encoding="utf-8"))["session"]


def _loaded_min(tmp_path: Path, value: float) -> float:
    """What the real load path makes of *value* in a hand-edited config.json.

    Both location helpers point at a directory of its own, so the file the PATCH
    wrote (``tmp_config``) is not the one read.
    """
    cfg_dir = tmp_path / "hand-edited"
    cfg_dir.mkdir()
    cfg_file = cfg_dir / "config.json"
    cfg_file.write_text(
        json.dumps({"session": {"idle_compaction_min_context_pct": value}}), encoding="utf-8"
    )
    with (
        unittest.mock.patch("kiro_crew.config.loader.config_dir", return_value=cfg_dir),
        unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg_file),
    ):
        return KiroCrewConfig.load().session.idle_compaction_min_context_pct


class TestIdleCompactionToggle:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", [True, False])
    async def test_a_boolean_is_stored(self, tmp_config, value) -> None:
        status, body = await _patch("session.idle_compaction", value)
        assert status == 200
        assert body["session"]["idle_compaction"] is value
        assert _session(tmp_config)["idle_compaction"] is value

    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", ["true", 1, 0, None])
    async def test_a_non_boolean_is_refused_and_nothing_is_written(self, tmp_config, value) -> None:
        status, _body = await _patch("session.idle_compaction", value)
        assert status == 400
        assert "idle_compaction" not in _session(tmp_config)


class TestIdleCompactionMinimumContext:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", [40, 50, 62.5, 90])
    async def test_an_in_range_value_is_stored_without_a_notice(self, tmp_config, value) -> None:
        status, body = await _patch(_MIN_PATH, value)
        assert status == 200
        assert "clamp_notice" not in body
        assert _session(tmp_config)["idle_compaction_min_context_pct"] == float(value)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("value", "stored"), [(0, 40.0), (39.5, 40.0), (90.5, 90.0), (500, 90.0)]
    )
    async def test_an_out_of_range_value_is_clamped_with_a_notice(
        self, tmp_config, tmp_path, value, stored
    ) -> None:
        status, body = await _patch(_MIN_PATH, value)
        assert status == 200
        assert body["clamp_notice"] == {
            "path": _MIN_PATH,
            "requested": float(value),
            "stored": stored,
            "min": IDLE_COMPACTION_MIN_CONTEXT_PCT_MIN,
            "max": IDLE_COMPACTION_MIN_CONTEXT_PCT_MAX,
        }
        assert _session(tmp_config)["idle_compaction_min_context_pct"] == stored
        # The write and a load of the same request agree, so the row never shows
        # a value that the next load would rewrite.
        assert _loaded_min(tmp_path, float(value)) == stored

    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", ["abc", float("nan"), True, None])
    async def test_a_non_number_is_refused_and_nothing_is_written(self, tmp_config, value) -> None:
        status, _body = await _patch(_MIN_PATH, value)
        assert status == 400
        assert "idle_compaction_min_context_pct" not in _session(tmp_config)
