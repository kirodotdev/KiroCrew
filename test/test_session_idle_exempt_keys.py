"""``session.idle_exempt_keys``: the idle clock skips a listed session, and nothing else does.

A listed key is never an idle-axis candidate, and the list is re-read at every sweep. The
exemption stops there: a closed tab still reaps a listed session on the owner-gone axis, and
the RSS recycle still resets one over the ceiling. A listed key no tab ever claimed is reaped on
neither axis. The loader keeps only non-empty key strings, within a count bound and a length
bound.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.config import KiroCrewConfig
from kiro_crew.config.loader import _build_session_config
from kiro_crew.config.sections import IDLE_EXEMPT_KEY_MAX_CHARS, IDLE_EXEMPT_KEYS_MAX, POOL_SIZE_MAX
from kiro_crew.session import SessionManager

EXEMPT_KEY = "dashboard:chat-1-1790000000"
# Extend the listed key so the test rejects prefix or substring exemptions.
ORDINARY_KEY = EXEMPT_KEY + ":suffix"
# A cron job's stable key, which no tab claims, and a per-run key of the same job.
CRON_KEY = "cron:job-1"
CRON_RUN_KEY = CRON_KEY + ":run-1"


def _provider_factory():
    def factory(session_key=None, agent=None, channel_id=None, **kwargs):
        provider = AsyncMock()
        provider.context_usage_pct = lambda: 0.0
        provider.has_active_turn = lambda: False
        return provider

    return factory


def _manager(*exempt_keys: str) -> SessionManager:
    cfg = KiroCrewConfig()
    cfg.session.idle_exempt_keys = list(exempt_keys)
    return SessionManager(cfg, provider_factory=_provider_factory())


async def _open_long_idle(manager: SessionManager, key: str) -> None:
    await manager.get_or_create(key)
    manager.release(key)
    async with manager._lock:
        manager._sessions[key].last_used = time.monotonic() - 10_000


@pytest.mark.asyncio
async def test_the_idle_clock_never_expires_a_listed_session() -> None:
    manager = _manager(EXEMPT_KEY)
    await _open_long_idle(manager, EXEMPT_KEY)
    await _open_long_idle(manager, ORDINARY_KEY)

    await manager._expire_idle(timeout_secs=60)

    assert EXEMPT_KEY in manager._sessions, "a listed session was reaped for being idle"
    assert ORDINARY_KEY not in manager._sessions, "an unlisted idle session must still expire"
    await manager.close_all()


@pytest.mark.asyncio
async def test_a_key_taken_off_the_list_expires_at_the_next_sweep() -> None:
    manager = _manager(EXEMPT_KEY)
    await _open_long_idle(manager, EXEMPT_KEY)
    await manager._expire_idle(timeout_secs=60)
    assert EXEMPT_KEY in manager._sessions

    manager._cfg.session.idle_exempt_keys = []
    await manager._expire_idle(timeout_secs=60)

    assert EXEMPT_KEY not in manager._sessions, "the list must be read at every sweep"
    await manager.close_all()


@pytest.mark.asyncio
async def test_closing_the_tab_still_reaps_a_listed_session() -> None:
    manager = _manager(EXEMPT_KEY)
    await manager.get_or_create(EXEMPT_KEY)
    manager.release(EXEMPT_KEY)
    manager.set_active_dashboard_slots({ORDINARY_KEY})

    await manager._expire_idle(timeout_secs=86_400)

    assert EXEMPT_KEY not in manager._sessions, "the exemption must not outlive the tab"
    await manager.close_all()


@pytest.mark.asyncio
async def test_a_listed_key_that_never_had_a_tab_outlives_the_sweep() -> None:
    manager = _manager(CRON_KEY)
    await _open_long_idle(manager, CRON_KEY)
    await _open_long_idle(manager, CRON_RUN_KEY)
    manager.set_active_dashboard_slots({EXEMPT_KEY})

    await manager._expire_idle(timeout_secs=60)

    assert CRON_KEY in manager._sessions, "no slot ever owned it, so neither axis may reap it"
    assert CRON_RUN_KEY not in manager._sessions, "an unlisted cron session must still expire"
    await manager.close_all()


@pytest.mark.asyncio
async def test_the_rss_recycle_still_resets_a_listed_session() -> None:
    cfg = MagicMock()
    cfg.session.pool_size = 0
    cfg.session.pool_agent = ""
    cfg.session.pool_ttl_secs = 0
    cfg.session.watchdog_rss_max_mb = 1000
    cfg.session.idle_exempt_keys = [EXEMPT_KEY]
    manager = SessionManager(cfg=cfg, provider_factory=None)
    session = MagicMock()
    session.semaphore.locked.return_value = False
    manager._sessions[EXEMPT_KEY] = session
    manager.reset = AsyncMock(return_value=True)
    manager.get_pid = MagicMock(return_value=4242)

    # The stubs are the /proc route's measurement, so pin that route: a Windows host would
    # otherwise measure the fake pid for real and read it as under the ceiling.
    with (
        patch("kiro_crew.session.platform_compat.IS_WINDOWS", False),
        patch("kiro_crew.session._build_child_map", return_value={}),
        patch("kiro_crew.session._rss_mb_from_tree", return_value=2048),
    ):
        await manager._rss_threshold_check()

    manager.reset.assert_awaited_once()
    assert manager.reset.await_args.args[0] == EXEMPT_KEY


def test_the_loader_keeps_only_non_empty_key_strings() -> None:
    loaded = _build_session_config(
        {"idle_exempt_keys": [" dashboard:a ", "", "  ", 7, None, "cron:b"]}
    )

    assert loaded.idle_exempt_keys == ["dashboard:a", "cron:b"]


def test_a_non_list_value_loads_as_no_exemptions() -> None:
    assert _build_session_config({"idle_exempt_keys": "dashboard:a"}).idle_exempt_keys == []
    assert _build_session_config({}).idle_exempt_keys == []


def test_the_bounds_are_the_numbers_the_help_text_and_docs_state() -> None:
    # The help text says 10 keys, docs/configuration.md and the config spec say 10 keys and 512
    # characters, and the count follows the warm pool's ceiling, so moving a bound must update them.
    assert IDLE_EXEMPT_KEYS_MAX == POOL_SIZE_MAX == 10
    assert IDLE_EXEMPT_KEY_MAX_CHARS == 512


def test_the_loader_keeps_only_the_first_bounded_number_of_keys(
    caplog: pytest.LogCaptureFixture,
) -> None:
    keys = [f"dashboard:{index}" for index in range(IDLE_EXEMPT_KEYS_MAX + 3)]

    with caplog.at_level(logging.WARNING, logger="kiro_crew.config.loader"):
        loaded = _build_session_config({"idle_exempt_keys": keys})

    assert loaded.idle_exempt_keys == keys[:IDLE_EXEMPT_KEYS_MAX]
    assert len(caplog.records) == 1
    assert f"and 3 past the {IDLE_EXEMPT_KEYS_MAX}-key limit" in caplog.text


def test_the_loader_drops_an_overlong_key_without_truncating_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    exact = "e" * IDLE_EXEMPT_KEY_MAX_CHARS
    overlong = "o" * (IDLE_EXEMPT_KEY_MAX_CHARS + 1)
    padded = "  " + "p" * IDLE_EXEMPT_KEY_MAX_CHARS + "  "

    with caplog.at_level(logging.WARNING, logger="kiro_crew.config.loader"):
        loaded = _build_session_config({"idle_exempt_keys": [exact, overlong, padded]})

    assert loaded.idle_exempt_keys == [exact, padded.strip()]
    assert overlong[:IDLE_EXEMPT_KEY_MAX_CHARS] not in loaded.idle_exempt_keys
    assert len(caplog.records) == 1
    assert f"dropped 1 key(s) longer than {IDLE_EXEMPT_KEY_MAX_CHARS} characters" in caplog.text


def test_an_overlong_key_does_not_consume_a_bounded_slot(
    caplog: pytest.LogCaptureFixture,
) -> None:
    overlong = "o" * (IDLE_EXEMPT_KEY_MAX_CHARS + 1)
    valid = [f"cron:{index}" for index in range(IDLE_EXEMPT_KEYS_MAX)]

    with caplog.at_level(logging.WARNING, logger="kiro_crew.config.loader"):
        loaded = _build_session_config({"idle_exempt_keys": [overlong, *valid]})

    assert loaded.idle_exempt_keys == valid
    assert len(caplog.records) == 1


def test_a_list_within_both_bounds_loads_without_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    keys = [" dashboard:a ", "cron:b"]

    with caplog.at_level(logging.WARNING, logger="kiro_crew.config.loader"):
        loaded = _build_session_config({"idle_exempt_keys": keys})

    assert loaded.idle_exempt_keys == ["dashboard:a", "cron:b"]
    assert caplog.records == []


def test_config_json_reaches_the_session_config(tmp_path: Path) -> None:
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({"session": {"idle_exempt_keys": [f" {EXEMPT_KEY} "]}}))

    with (
        patch("kiro_crew.config.loader.config_path", return_value=config_file),
        patch(
            "kiro_crew.config.loader.config_local_path", return_value=tmp_path / "config.local.json"
        ),
    ):
        loaded = KiroCrewConfig.load()

    assert loaded.session.idle_exempt_keys == [EXEMPT_KEY]
