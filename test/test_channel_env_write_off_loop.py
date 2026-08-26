"""Every channel token save writes ``.env`` off the loop AND drains before unlocking.

``_write_env_updates`` stats and reads the whole ``.env``, re-parses it line by
line, then writes an owner-locked temp file and renames it. All of it is
synchronous file I/O, and all six channel config-save handlers are ``async``
request handlers on the shared gateway loop.

Two separate properties are pinned here, because the offload alone is not the
whole contract:

1. the write runs on a worker, not on the gateway loop -- asserted on the
   thread the write ACTUALLY ran on, captured by wrapping
   ``_write_env_updates`` itself, so it holds regardless of how a caller
   reaches it;
2. a cancelled save drains that worker before releasing ``_get_config_lock()``
   -- the property a bare ``await asyncio.to_thread(...)`` does not have, and
   the one the last test in this module covers.

``.env`` and ``config.json`` are redirected into ``tmp_path`` and every token
validator is stubbed to accept, so nothing here touches the network or a real
credential file.
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

#: The channels whose saves are driven end to end here. Named so a
#: parametrisation that silently loses one is a failure, not a smaller run.
PINNED = {"slack", "discord", "webex", "telegram"}


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
    """Every channel this module claims to pin is still parametrised.

    A parametrisation that quietly lost a channel would keep passing while
    that channel regressed to a bare offload, which is the exact way a
    per-site convention drifts back apart.
    """
    covered = {c[0] for c in CHANNELS}
    assert PINNED <= covered, f"channels dropped from the table: {PINNED - covered}"


def test_cancelling_a_save_drains_the_env_write_before_releasing_the_lock(
    monkeypatch, tmp_path: Path
) -> None:
    """A cancelled save must not hand the config lock on mid-write.

    Every channel save holds ``_get_config_lock()`` across the ``.env`` write, and
    a thread cannot be cancelled. Without the drain, cancelling the request
    unwinds the ``async with`` while the worker is still rewriting the file, so
    the next channel save enters the critical section against a file still being
    replaced and writes it back from lines it read before the first write landed
    -- discarding whichever credential that save was persisting.

    Ordering is forced with events, never slept for: the worker parks inside the
    write, the caller is cancelled while it is parked, and a second writer then
    tries to proceed. It must not get through until the first worker finishes.
    """
    from kiro_crew.dashboard.handlers.agents import _get_config_lock

    in_write = threading.Event()
    finish = threading.Event()
    order: list[str] = []

    def _slow_write(_updates):
        order.append("worker-start")
        in_write.set()
        finish.wait(timeout=10)
        order.append("worker-end")

    monkeypatch.setattr(mod, "_write_env_updates", _slow_write)

    async def _first():
        async with _get_config_lock():
            await mod._write_env_off_loop({"A": "1"})

    async def _second():
        async with _get_config_lock():
            order.append("second-ran")

    async def _run() -> tuple[bool, list[str]]:
        first = asyncio.create_task(_first())
        assert await asyncio.to_thread(in_write.wait, 10), "the worker never entered"

        first.cancel()
        second = asyncio.create_task(_second())
        # Yield generously rather than sleeping: if the lock had been released the
        # second writer would have run by now.
        for _ in range(200):
            await asyncio.sleep(0)
        early = "second-ran" in order

        finish.set()
        try:
            await first
        except asyncio.CancelledError:
            pass
        await asyncio.wait_for(second, timeout=10)
        return early, order

    early, seen = asyncio.run(_run())

    assert not early, (
        "the config lock was handed to the next channel save while the cancelled "
        "one's worker was still rewriting .env: %r" % (seen,)
    )
    assert seen.index("worker-end") < seen.index(
        "second-ran"
    ), "the second save entered before the first write finished: %r" % (seen,)
