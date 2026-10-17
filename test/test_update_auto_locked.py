"""The auto-update toggle serializes against writers outside this process."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from kiro_crew.config import loader as cfg_loader


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "home"
    d.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(d))
    cfg = d / "config.json"
    cfg.write_text(
        json.dumps({"timezone": "UTC", "auto_update": False, "session": {"timeout_secs": 7200}}),
        encoding="utf-8",
    )
    return cfg


def test_a_writer_landing_mid_update_is_not_lost(home: Path) -> None:
    """The lock spans the read AND the write, so the competing setting survives.

    The competing writer is driven from inside the `mutate` callback -- i.e. at the exact moment
    the endpoint holds the config open -- because that is the window the old in-process
    asyncio lock left unprotected. It runs in a thread and must BLOCK on the advisory lock
    until the callback returns.
    """
    landed = threading.Event()

    def competing_writer() -> None:
        # A separate lock acquisition, exactly as another process would take it.
        cfg_loader.update_config_locked(
            home,
            mutate=lambda d: {**d, "timezone": "Asia/Shanghai"},
        )
        landed.set()

    def _toggle(data: dict) -> dict:
        data["auto_update"] = True
        t = threading.Thread(target=competing_writer)
        t.start()
        # The competing writer must NOT have completed while this callback holds the lock.
        # A 0.5s window is generous; if it lands, the lock does not span the callback.
        assert not landed.wait(timeout=0.5), "the competing write landed inside the lock hold"
        return data

    cfg_loader.update_config_locked(home, mutate=_toggle)
    # Now it may proceed.
    assert landed.wait(timeout=10), "the competing writer never completed"

    on_disk = json.loads(home.read_text(encoding="utf-8"))
    assert on_disk["auto_update"] is True, "the endpoint's own edit was lost"
    assert on_disk["timezone"] == "Asia/Shanghai", "the competing writer's setting was reverted"
    assert on_disk["session"]["timeout_secs"] == 7200, "an untouched setting was dropped"


def test_an_unreadable_config_refuses_instead_of_resetting(home: Path) -> None:
    """Fail-closed: a truncated config must not be replaced by a single-key file.

    This is the data-loss shape `read_config_for_update` exists to prevent, and it still holds
    through the locked primitive because `on_corrupt` defaults to "fail".
    """
    home.write_text('{"timezone": "UTC", "auto_upda', encoding="utf-8")
    before = home.read_text(encoding="utf-8")

    with pytest.raises(cfg_loader.ConfigReadError):
        cfg_loader.update_config_locked(home, mutate=lambda d: {**d, "auto_update": True})

    assert home.read_text(encoding="utf-8") == before, "the unreadable config was overwritten"
