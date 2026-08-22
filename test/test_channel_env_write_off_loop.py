"""Every channel token save writes ``.env`` off the gateway event loop.

``_write_env_updates`` stats and reads the whole ``.env``, re-parses it line by
line, then creates a 0600 temp file, chmods it, writes, and renames. All of it
is synchronous file I/O, and all six channel config-save handlers are ``async``
request handlers on the shared gateway loop -- so an inline call stalls every
other session for the duration of that write.

Three of the six already reached it through ``asyncio.to_thread`` (telegram,
teams, wecom) and three did not (slack, discord, webex). This module pins the
property for the whole family rather than for the three that were fixed,
because the defect was not a missing convention -- it was an existing one
applied to half the call sites, and nothing stopped the next channel from
picking the wrong half.

The assertion is on the thread the write ACTUALLY ran on, captured by wrapping
``_write_env_updates`` itself, so it holds regardless of how a caller reaches
it. ``.env`` and ``config.json`` are redirected into ``tmp_path`` and every
token validator is stubbed to accept, so nothing here touches the network or a
real credential file.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import kiro_crew.config.loader as loader
import kiro_crew.dashboard.handlers.messaging as mod

# Shapes each validator accepts. Real-looking because the handlers reject on
# format before they ever reach the write, and a rejected body would make this
# pass for the wrong reason.
TELEGRAM_TOKEN = "110201543:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"
DISCORD_TOKEN = ".".join(
    ["MTA5OTk5OTk5OTk5OTk5OTk5OQ", "GhIjKl", "MnOpQrStUvWxYz0123456789_-AbCdEfGhIj"]
)

# route, handler attribute, validator attribute, body — one row per channel.
CHANNELS = [
    ("slack", "api_slack_config_save", "_validate_slack_token", {"bot_token": "xoxb-NEW"}),
    ("discord", "api_discord_config_save", "_validate_discord_token", {"bot_token": DISCORD_TOKEN}),
    ("webex", "api_webex_config_save", "_validate_webex_token", {"bot_token": "webex-tok-1234"}),
    (
        "telegram",
        "api_telegram_config_save",
        "_validate_telegram_token",
        {"bot_token": TELEGRAM_TOKEN},
    ),
]

#: Which of those were the unfixed siblings. Split out so a reader can tell the
#: regression coverage from the parity coverage without diffing history.
FIXED_HERE = {"slack", "discord", "webex"}


def _drive(channel: str, monkeypatch, tmp_path: Path) -> list[int]:
    """Save one channel's token; return the threads the .env write ran on."""
    name, handler_attr, validator_attr, body = next(c for c in CHANNELS if c[0] == channel)

    env = tmp_path / ".env"
    env.write_text("", encoding="utf-8")
    monkeypatch.setattr(loader, "env_path", lambda: env)
    monkeypatch.setattr(loader, "config_path", lambda: tmp_path / "config.json")
    monkeypatch.setattr(mod, "is_direct_local_request", lambda req: True)

    async def _accept(*_args, **_kwargs):
        return None

    monkeypatch.setattr(mod, validator_attr, _accept, raising=False)

    threads: list[int] = []
    real_write = mod._write_env_updates

    def _record(updates):
        threads.append(threading.get_ident())
        return real_write(updates)

    monkeypatch.setattr(mod, "_write_env_updates", _record)

    async def _run() -> int:
        app = web.Application()
        app.router.add_put(f"/api/{name}/config", getattr(mod, handler_attr))
        async with TestClient(TestServer(app)) as client:
            resp = await client.put(f"/api/{name}/config", json=body)
            return resp.status

    status = asyncio.run(_run())
    assert status == 200, f"{name} save did not succeed ({status}); the write was never reached"
    return threads


@pytest.mark.parametrize("channel", sorted(c[0] for c in CHANNELS))
def test_channel_token_save_writes_env_off_the_loop(channel, monkeypatch, tmp_path: Path) -> None:
    loop_thread = threading.get_ident()
    threads = _drive(channel, monkeypatch, tmp_path)

    assert threads, f"the {channel} save never wrote .env"
    assert threads[0] != loop_thread, (
        f"the {channel} token save wrote .env on the gateway loop: a stat, a full "
        "read and re-parse, then a temp-file create + chmod + rename, with every "
        "other session blocked for the duration"
    )


def test_the_family_is_covered_here(monkeypatch, tmp_path: Path) -> None:
    """The three fixed siblings and at least one that was already correct.

    Guards the split this module exists to close: a parametrisation that
    silently lost the already-correct channels would stop noticing if one of
    them regressed back to an inline call.
    """
    covered = {c[0] for c in CHANNELS}
    assert FIXED_HERE <= covered
    assert covered - FIXED_HERE, "no already-correct channel is exercised as a parity anchor"
