"""A vault read that meets a Windows sharing violation is retried.

On Windows a reader cannot open ``secrets.enc`` while a writer's ``os.replace``
of it is in flight, and the open raises ``PermissionError``. ``_load_entries``
reads through ``atomic_write.read_bytes_with_retry``, so that window is waited
out off the event loop instead of failing the read. A refusal that outlasts the
retry budget, and a corrupt store, still fail as before.

The share-deny is simulated on both ``Path.read_bytes`` and ``Path.read_text``,
so a read through either boundary meets it.
"""

from __future__ import annotations

import asyncio
import logging
import types
from pathlib import Path

import pytest

from kiro_crew.acp import kas_secret_storage as kss
from kiro_crew.secrets.vault import SecretVault

_STORE_NAME = "secrets.enc"
_TOKENS_KEY = f"kiro.mcp.{'cd' * 32}.tokens"
_PLACEHOLDER = '{"access_token":"placeholder-value","token_type":"Bearer"}'


class _ShareDeny:
    """Refuse the first ``fail_times`` reads of ``secrets.enc``, then read normally.

    ``fail_times=None`` refuses every read, as a read-deny ACL would.
    ``attempts`` counts the store reads that were made.
    """

    def __init__(self, fail_times: int | None) -> None:
        self._fail_times = fail_times
        self._refused = 0
        self.attempts = 0
        real_bytes = Path.read_bytes
        real_text = Path.read_text
        state = self

        def _deny(this: Path) -> bool:
            if this.name != _STORE_NAME:
                return False
            state.attempts += 1
            if state._fail_times is None or state._refused < state._fail_times:
                state._refused += 1
                return True
            return False

        def patch_bytes(this, *a, **k):
            if _deny(this):
                raise PermissionError(13, "Permission denied", str(this))
            return real_bytes(this, *a, **k)

        def patch_text(this, *a, **k):
            if _deny(this):
                raise PermissionError(13, "Permission denied", str(this))
            return real_text(this, *a, **k)

        self.patch_bytes = patch_bytes
        self.patch_text = patch_text


def _no_sleep_clock() -> types.SimpleNamespace:
    """``atomic_write``'s ``time`` binding with only ``sleep`` stubbed."""
    import time as _real_time

    clock = types.SimpleNamespace()
    for name in dir(_real_time):
        if not name.startswith("__"):
            setattr(clock, name, getattr(_real_time, name))
    clock.sleep = lambda _s: None
    return clock


def _arm_windows(monkeypatch, deny: _ShareDeny) -> None:
    from kiro_crew import atomic_write, platform_compat

    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(atomic_write.platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(atomic_write, "time", _no_sleep_clock())
    monkeypatch.setattr(Path, "read_bytes", deny.patch_bytes)
    monkeypatch.setattr(Path, "read_text", deny.patch_text)


def test_a_transient_share_deny_on_the_store_is_retried_by_every_reader(tmp_path, monkeypatch):
    vault = SecretVault(tmp_path)
    vault.set_sync("a", "one")
    vault.set_sync("b", "two")

    deny = _ShareDeny(fail_times=3)
    _arm_windows(monkeypatch, deny)
    got = vault.get("a")
    assert got is not None and got.reveal() == "one"
    assert deny.attempts == 4

    for read in (
        lambda: vault.get_with_revision("a")[0].reveal(),
        lambda: vault.get_many(["a", "b"])["b"].reveal(),
        lambda: vault.list_names(),
    ):
        deny = _ShareDeny(fail_times=2)
        _arm_windows(monkeypatch, deny)
        assert read() in ("one", "two", ["a", "b"])
        assert deny.attempts == 3


def test_a_share_deny_that_outlasts_the_budget_still_raises(tmp_path, monkeypatch):
    vault = SecretVault(tmp_path)
    vault.set_sync("a", "one")

    deny = _ShareDeny(fail_times=None)
    _arm_windows(monkeypatch, deny)
    with pytest.raises(PermissionError):
        vault.get("a")
    from kiro_crew import atomic_write

    assert deny.attempts == atomic_write._REPLACE_MAX_ATTEMPTS


def test_a_corrupt_store_is_read_once_and_still_refused(tmp_path, monkeypatch):
    vault = SecretVault(tmp_path)
    vault.set_sync("a", "one")
    (tmp_path / ".vault" / _STORE_NAME).write_bytes(b"\xff not json")

    deny = _ShareDeny(fail_times=0)
    _arm_windows(monkeypatch, deny)
    with pytest.raises(UnicodeDecodeError):
        vault.get("a")
    assert deny.attempts == 1


@pytest.mark.asyncio
async def test_an_engine_get_that_meets_a_token_save_is_answered(tmp_path, monkeypatch, caplog):
    """The issue's path: ``_kiro/secret/get`` through ``SecretStorageServer``."""
    writer = kss.SecretStorageServer(kss.McpGrantStore(tmp_path))
    await writer.answer(kss.METHOD_SECRET_STORE, {"key": _TOKENS_KEY, "value": _PLACEHOLDER})

    deny = _ShareDeny(fail_times=3)
    _arm_windows(monkeypatch, deny)
    reader = kss.SecretStorageServer(kss.McpGrantStore(tmp_path))
    with caplog.at_level(logging.WARNING, logger=kss.logger.name):
        answer = await asyncio.wait_for(
            reader.answer(kss.METHOD_SECRET_GET, {"key": _TOKENS_KEY}), timeout=30
        )
    assert answer == {"value": _PLACEHOLDER}
    assert "MCP grant store failed" not in caplog.text


@pytest.mark.asyncio
async def test_an_engine_get_on_a_persistently_unreadable_store_is_refused(tmp_path, monkeypatch):
    writer = kss.SecretStorageServer(kss.McpGrantStore(tmp_path))
    await writer.answer(kss.METHOD_SECRET_STORE, {"key": _TOKENS_KEY, "value": _PLACEHOLDER})

    _arm_windows(monkeypatch, _ShareDeny(fail_times=None))
    reader = kss.SecretStorageServer(kss.McpGrantStore(tmp_path))
    with pytest.raises(kss.SecretStorageRefused, match="MCP grant store error"):
        await asyncio.wait_for(
            reader.answer(kss.METHOD_SECRET_GET, {"key": _TOKENS_KEY}), timeout=30
        )
