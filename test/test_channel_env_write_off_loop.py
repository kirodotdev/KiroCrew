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
import os
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

    # A landed save copies its credentials into os.environ. Put every key it
    # touched back as it was, so a token saved here never reaches a later test.
    environ_before = dict(os.environ)
    try:
        status = asyncio.run(_run())
    finally:
        for key in set(os.environ) | set(environ_before):
            if key not in environ_before:
                os.environ.pop(key, None)
            elif os.environ.get(key) != environ_before[key]:
                os.environ[key] = environ_before[key]
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


# ── the shared rollback step ─────────────────────────────────────────────────

#: Every settings owner that commits config.json and then .env.
_SAVER_OWNERS = ("slack", "teams", "webex", "wecom", "feishu", "discord", "telegram")


@pytest.mark.parametrize("owner", _SAVER_OWNERS)
def test_every_saver_writes_env_through_the_shared_rollback_step(owner: str) -> None:
    """A saver reaches ``.env`` only through ``_write_env_or_roll_back``.

    The write-then-roll-back-then-sync sequence exists once. A saver that called
    ``_write_env_off_loop`` itself would be a second copy, and a copy is how one
    saver ends up with the rollback but not the cancellation guard.
    """
    source = (
        Path(mod.__file__).resolve().parent.parent / "messaging_api" / f"{owner}_settings.py"
    ).read_text(encoding="utf-8")
    assert source.count("await _write_env_or_roll_back(") == 1
    assert "_write_env_off_loop(" not in source
    assert "asyncio.shield(" not in source


#: How long a wait the test itself unblocks may take before the run counts as
#: lost: the bound this module's other self-released waits already use.
_LOST_RUN_CEILING_SECS = 10.0


def _rollback_probe() -> tuple[list[str], object]:
    calls: list[str] = []

    async def _rollback() -> None:
        calls.append("rollback")

    return calls, _rollback


def test_a_failed_write_rolls_back_and_raises(monkeypatch) -> None:
    async def _boom(_updates):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(mod, "_write_env_off_loop", _boom)
    calls, rollback = _rollback_probe()
    with pytest.raises(OSError):
        asyncio.run(mod._write_env_or_roll_back({"KC_PROBE_TOKEN": "x"}, rollback))
    assert calls == ["rollback"]


def test_a_failed_write_with_no_config_commit_just_raises(monkeypatch) -> None:
    async def _boom(_updates):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(mod, "_write_env_off_loop", _boom)
    with pytest.raises(OSError):
        asyncio.run(mod._write_env_or_roll_back({"KC_PROBE_TOKEN": "x"}, None))


@pytest.mark.parametrize("write_fails", [False, True])
def test_a_cancelled_save_rolls_back_only_when_the_write_failed(
    write_fails: bool, monkeypatch
) -> None:
    """Cancel the caller while the write is parked; the write then lands or fails.

    A write that landed pairs with the committed config, so rolling the config
    back would split the pair. A write that failed leaves the config ahead of
    ``.env``, so it is undone. The cancellation propagates in both cases.
    """
    release = asyncio.Event()

    async def _parked(_updates):
        await asyncio.wait_for(release.wait(), timeout=_LOST_RUN_CEILING_SECS)
        if write_fails:
            raise OSError(5, "I/O error")

    monkeypatch.setattr(mod, "_write_env_off_loop", _parked)
    calls, rollback = _rollback_probe()

    async def _run() -> bool:
        save = asyncio.create_task(mod._write_env_or_roll_back({"KC_PROBE_TOKEN": "x"}, rollback))
        for _ in range(5):
            await asyncio.sleep(0)
        save.cancel()
        for _ in range(5):
            await asyncio.sleep(0)
        release.set()
        try:
            await asyncio.wait_for(save, timeout=_LOST_RUN_CEILING_SECS)
        except asyncio.CancelledError:
            return True
        return False

    assert asyncio.run(_run()) is True
    assert calls == (["rollback"] if write_fails else [])


def test_a_landed_write_syncs_the_process_environment(monkeypatch) -> None:
    async def _ok(_updates):
        return None

    monkeypatch.setattr(mod, "_write_env_off_loop", _ok)
    calls, rollback = _rollback_probe()
    with monkeypatch.context() as env:
        env.setenv("KC_PROBE_CLEARED", "old")
        # setenv first records the variable's original state (absent here), so
        # the undo removes whatever the helper writes; delenv then clears it.
        env.setenv("KC_PROBE_SET", "unset-before-the-save")
        env.delenv("KC_PROBE_SET")
        asyncio.run(
            mod._write_env_or_roll_back({"KC_PROBE_SET": "new", "KC_PROBE_CLEARED": None}, rollback)
        )
        assert os.environ.get("KC_PROBE_SET") == "new"
        assert "KC_PROBE_CLEARED" not in os.environ
    # Once the scoped undo has run, neither probe is left behind for a later test.
    assert "KC_PROBE_SET" not in os.environ
    assert "KC_PROBE_CLEARED" not in os.environ
    assert calls == []


@pytest.mark.parametrize("channel", ["discord", "telegram"])
def test_the_config_and_env_writes_run_under_the_live_config_hold(
    channel: str, monkeypatch, tmp_path: Path
) -> None:
    """The Discord and Telegram saves roll their config write back on a failed
    ``.env`` write, so, like the other savers, both writes run under
    ``live.hold()``: the watcher must not apply a config the failing write may
    yet undo."""
    from kiro_crew.config import live

    held_at: list[str] = []
    watcher = live.watch()
    real_json_write = loader.write_config_atomically
    real_env_write = mod._write_env_off_loop

    def _json_write(path, data, **kw):
        held_at.append(f"config:{watcher._hold_depth}")
        real_json_write(path, data, **kw)

    async def _env_write(updates):
        held_at.append(f"env:{watcher._hold_depth}")
        await real_env_write(updates)

    monkeypatch.setattr(loader, "write_config_atomically", _json_write)
    monkeypatch.setattr(mod, "_write_env_off_loop", _env_write)
    (tmp_path / "config.json").write_text(
        '{"%s": {"enabled": false, "bot_token": "legacy"}}' % channel, encoding="utf-8"
    )
    _drive(channel, monkeypatch, tmp_path)
    assert held_at == ["config:1", "env:1"], held_at
    assert watcher._hold_depth == 0
