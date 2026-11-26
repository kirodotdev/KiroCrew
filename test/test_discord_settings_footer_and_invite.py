"""The Discord settings API's timing-footer switch and the bot's invite links.

* ``show_timing_footer`` reads back from GET, saves through PUT as a boolean
  only, and needs no restart (the dispatcher reads it per turn).
* GET carries the two invite links that ``discord/install_url.py`` builds from
  the application id READY gave the client of the registered Discord transport,
  and none when no transport is registered, before READY, or when the id is not
  a snowflake. No token is in them.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

import kiro_crew.config.loader as loader
from kiro_crew.discord.install_url import build_install_url

APP_ID = "123456789012345678"

# Discord's three-segment token shape, joined at runtime so no literal here
# matches GitHub secret scanning's Discord bot-token pattern.
TOKEN = ".".join(["MTA5OTk5OTk5OTk5OTk5OTk5OQ", "GhIjKl", "MnOpQrStUvWxYz0123456789_-AbCdEfGhIj"])


def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, config: dict[str, Any]) -> None:
    env = tmp_path / ".env"
    env.write_text(f"DISCORD_BOT_TOKEN={TOKEN}\n", encoding="utf-8")
    (tmp_path / "config.json").write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setattr(loader, "env_path", lambda: env)
    monkeypatch.setattr(loader, "config_path", lambda: tmp_path / "config.json")
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)


def _get(state: object) -> tuple[dict[str, Any], str]:
    import kiro_crew.dashboard.handlers.messaging as mod

    req = make_mocked_request("GET", "/api/discord/config", app={"state": state})
    resp = asyncio.run(mod.api_discord_config_get(req))
    assert resp.status == 200
    return json.loads(resp.text), resp.text


def _put(monkeypatch: pytest.MonkeyPatch, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    import kiro_crew.dashboard.handlers.messaging as mod

    monkeypatch.setattr(mod, "is_direct_local_request", lambda req: True)

    async def _run() -> tuple[int, dict[str, Any]]:
        app = web.Application()
        app.router.add_put("/api/discord/config", mod.api_discord_config_save)
        async with TestClient(TestServer(app)) as client:
            resp = await client.put("/api/discord/config", json=body)
            return resp.status, await resp.json()

    return asyncio.run(_run())


class TestTimingFooter:
    def test_get_reads_the_stored_switch(self, tmp_path: Path, monkeypatch) -> None:
        _isolate(monkeypatch, tmp_path, {"discord": {"show_timing_footer": False}})
        body, _ = _get(SimpleNamespace(discord_connected=False, discord_connect_error=""))
        assert body["show_timing_footer"] is False

    def test_get_defaults_it_on(self, tmp_path: Path, monkeypatch) -> None:
        _isolate(monkeypatch, tmp_path, {"discord": {}})
        body, _ = _get(SimpleNamespace(discord_connected=False, discord_connect_error=""))
        assert body["show_timing_footer"] is True

    def test_save_stores_it_without_a_restart(self, tmp_path: Path, monkeypatch) -> None:
        _isolate(monkeypatch, tmp_path, {"discord": {}})
        status, body = _put(monkeypatch, {"show_timing_footer": False})
        assert status == 200
        assert body["restart_required"] is False
        stored = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
        assert stored["discord"]["show_timing_footer"] is False

    def test_saving_the_default_writes_nothing(self, tmp_path: Path, monkeypatch) -> None:
        """An unset switch compares as on, so saving on stages no change."""
        _isolate(monkeypatch, tmp_path, {"discord": {}})
        status, _ = _put(monkeypatch, {"show_timing_footer": True})
        assert status == 200
        stored = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
        assert "show_timing_footer" not in stored["discord"]

    @pytest.mark.parametrize(
        "toggle,stored,sent",
        [
            ("show_timing_footer", None, False),
            ("reactions_enabled", None, False),
            ("show_thinking", "yes", True),
        ],
    )
    def test_a_save_over_a_malformed_stored_value_lands(
        self, tmp_path: Path, monkeypatch, toggle: str, stored: object, sent: bool
    ) -> None:
        """GET shows the default for a wrong-typed value, so a save differing from
        that default must be written, not compared against the raw value's truthiness.
        """
        _isolate(monkeypatch, tmp_path, {"discord": {toggle: stored}})
        body, _ = _get(SimpleNamespace(discord_connected=False, discord_connect_error=""))
        assert body[toggle] is (not sent)
        status, _ = _put(monkeypatch, {toggle: sent})
        assert status == 200
        stored_after = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
        assert stored_after["discord"][toggle] is sent

    @pytest.mark.parametrize("value", ["false", 0, None, [False]])
    def test_save_refuses_a_non_boolean(self, tmp_path: Path, monkeypatch, value) -> None:
        _isolate(monkeypatch, tmp_path, {"discord": {}})
        status, body = _put(monkeypatch, {"show_timing_footer": value})
        assert status == 400
        assert body["error"] == "show_timing_footer must be a boolean"
        stored = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
        assert "show_timing_footer" not in stored["discord"]


def _state_with(app_id: str | None) -> SimpleNamespace:
    """A dashboard state whose registered Discord transport's client has *app_id*.

    ``None`` registers no Discord transport at all (never started, or closed).
    """
    transports: dict[str, Any] = {}
    if app_id is not None:
        transports["discord"] = SimpleNamespace(client=SimpleNamespace(application_id=app_id))
    return SimpleNamespace(
        discord_connected=True, discord_connect_error="", channel_transports=transports
    )


class TestInviteLinks:
    def test_the_running_bot_gets_both_links(self, tmp_path: Path, monkeypatch) -> None:
        _isolate(monkeypatch, tmp_path, {"discord": {"enabled": True}})
        body, text = _get(_state_with(APP_ID))
        assert body["install_url"] == build_install_url(APP_ID)
        assert body["install_url_dm_only"] == build_install_url(APP_ID, dm_only=True)
        assert body["install_url"] != body["install_url_dm_only"]
        assert TOKEN not in text

    @pytest.mark.parametrize(
        "app_id",
        [None, "", "12345&permissions=8", "not-a-snowflake"],
        ids=["no-live-transport", "before-ready", "query-injection", "wrong-kind"],
    )
    def test_no_link_without_a_live_snowflake(
        self, tmp_path: Path, monkeypatch, app_id: str | None
    ) -> None:
        _isolate(monkeypatch, tmp_path, {"discord": {"enabled": True}})
        body, _ = _get(_state_with(app_id))
        assert (body["install_url"], body["install_url_dm_only"]) == ("", "")

    def test_the_links_follow_the_registered_transport(self, tmp_path: Path, monkeypatch) -> None:
        """A close drops the transport, and a replacement start registers its own.

        The links are read from whichever transport is registered at GET time, so
        a closed client leaves none behind and a replacement's links survive the
        superseded client's teardown.
        """
        _isolate(monkeypatch, tmp_path, {"discord": {"enabled": True}})
        state = _state_with(APP_ID)
        assert _get(state)[0]["install_url"] == build_install_url(APP_ID)
        state.channel_transports.pop("discord")
        assert _get(state)[0]["install_url"] == ""
        other = "223456789012345678"
        state.channel_transports["discord"] = SimpleNamespace(
            client=SimpleNamespace(application_id=other)
        )
        assert _get(state)[0]["install_url"] == build_install_url(other)
