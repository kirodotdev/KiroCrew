"""A hand-edited stored ``soft_threshold_pct`` must not turn a channel save into a 500.

``config.json`` is a file an operator can edit by hand. The Telegram, Discord and
WeCom saves compare the requested threshold with the stored one; a stored value
that ``int()`` cannot read (``null``, a word) reads as the default instead
of raising out of the handler, the same rule the Feishu and Webex saves follow.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import kiro_crew.config.loader as loader

CHANNELS = [
    ("/api/telegram/config", "api_telegram_config_save", "telegram"),
    ("/api/discord/config", "api_discord_config_save", "discord"),
    ("/api/wecom/config", "api_wecom_config_save", "wecom"),
]


def _save(monkeypatch, tmp_path: Path, route: str, handler: str, body: dict) -> tuple[int, dict]:
    import kiro_crew.dashboard.handlers.messaging as mod

    env = tmp_path / ".env"
    if not env.exists():
        env.write_text("", encoding="utf-8")
    monkeypatch.setattr(loader, "env_path", lambda: env)
    monkeypatch.setattr(loader, "config_path", lambda: tmp_path / "config.json")
    monkeypatch.setattr(mod, "is_direct_local_request", lambda req: True)

    async def _run() -> tuple[int, dict]:
        app = web.Application()
        app.router.add_put(route, getattr(mod, handler))
        async with TestClient(TestServer(app)) as client:
            resp = await client.put(route, json=body)
            return resp.status, await resp.json()

    return asyncio.run(_run())


@pytest.mark.parametrize("stored", [None, "eighty"])
@pytest.mark.parametrize("route,handler,section", CHANNELS, ids=[c[2] for c in CHANNELS])
def test_a_malformed_stored_threshold_reads_as_the_default(
    tmp_path, monkeypatch, route, handler, section, stored
):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({section: {"soft_threshold_pct": stored}}), encoding="utf-8")

    status, payload = _save(monkeypatch, tmp_path, route, handler, {"soft_threshold_pct": 70})

    assert status == 200, payload
    assert json.loads(config.read_text(encoding="utf-8"))[section]["soft_threshold_pct"] == 70


@pytest.mark.parametrize("route,handler,section", CHANNELS, ids=[c[2] for c in CHANNELS])
def test_a_numeric_string_stored_threshold_is_the_value_compared(
    tmp_path, monkeypatch, route, handler, section
):
    # The loader reads "75" as 75, so saving 80 is a change and must be written.
    config = tmp_path / "config.json"
    config.write_text(json.dumps({section: {"soft_threshold_pct": "75"}}), encoding="utf-8")

    status, payload = _save(monkeypatch, tmp_path, route, handler, {"soft_threshold_pct": 80})

    assert status == 200, payload
    assert json.loads(config.read_text(encoding="utf-8"))[section]["soft_threshold_pct"] == 80


@pytest.mark.parametrize("route,handler,section", CHANNELS, ids=[c[2] for c in CHANNELS])
def test_saving_the_default_over_a_null_stored_threshold_succeeds(
    tmp_path, monkeypatch, route, handler, section
):
    # 80 is the default the malformed value reads as, so it is "no change": the
    # save answers without a 500 and leaves the stored value as it was.
    config = tmp_path / "config.json"
    config.write_text(json.dumps({section: {"soft_threshold_pct": None}}), encoding="utf-8")

    status, payload = _save(monkeypatch, tmp_path, route, handler, {"soft_threshold_pct": 80})

    assert status == 200, payload
