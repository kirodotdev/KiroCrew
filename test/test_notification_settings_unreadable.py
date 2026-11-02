"""A settings file that cannot be read at boot is never overwritten by the next update.

Pins the boot path of ``ChannelSettings``: a UTF-8 BOM file reads, a file that a
transient error kept from loading is read again before the first write, and one
that still cannot be read is renamed to ``.corrupt-<stamp>`` before the write
replaces it, so the stored mutes survive on disk.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from kiro_crew.notifications.bus import MONITOR_CHANNEL
from kiro_crew.notifications.settings import ChannelSettings

_STORED = {"channel_settings": {"a.one": {"muted": True}, "b.two": {"priority": "critical"}}}


@pytest.fixture()
def path(monkeypatch, tmp_path) -> Path:
    monkeypatch.setattr("kiro_crew.notifications.settings.config_dir", lambda: tmp_path)
    return tmp_path / "notification_settings.json"


def _channels(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))["channel_settings"]


def _asides(path: Path) -> list[Path]:
    return sorted(path.parent.glob(f"{path.name}.corrupt-*"))


def test_a_utf8_bom_file_loads_and_survives_the_next_update(path):
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps(_STORED).encode("utf-8"))
    settings = ChannelSettings()
    assert settings.get("a.one") == {"muted": True}
    settings.update("c.three", muted=True)
    assert set(_channels(path)) == {"a.one", "b.two", "c.three", MONITOR_CHANNEL}
    assert _asides(path) == []


def test_a_read_error_only_at_boot_keeps_every_stored_mute(path, monkeypatch):
    path.write_text(json.dumps(_STORED), encoding="utf-8")
    real = Path.read_text

    def locked(self, *args, **kwargs):
        if self == path:
            raise PermissionError(13, "being used by another process", str(self))
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", locked)
    settings = ChannelSettings()
    monkeypatch.setattr(Path, "read_text", real)
    assert settings.all_settings() == {}

    settings.update("c.three", muted=True)

    assert set(_channels(path)) == {"a.one", "b.two", "c.three", MONITOR_CHANNEL}
    assert settings.get("a.one") == {"muted": True}
    assert _asides(path) == []


def test_an_unparseable_file_is_kept_aside_and_the_update_still_lands(path, caplog):
    raw = b"\xff\xfe" + json.dumps(_STORED).encode("utf-16-le")
    path.write_bytes(raw)
    settings = ChannelSettings()
    assert settings.all_settings() == {}

    with caplog.at_level(logging.WARNING, logger="kiro_crew.notifications.settings"):
        entry = settings.update("c.three", muted=True)

    assert entry == {"muted": True}
    assert set(_channels(path)) == {"c.three", MONITOR_CHANNEL}
    asides = _asides(path)
    assert len(asides) == 1
    assert asides[0].read_bytes() == raw
    assert any(asides[0].name in record.getMessage() for record in caplog.records)

    settings.update("d.four", muted=True)
    assert len(_asides(path)) == 1


def test_a_missing_file_boots_with_defaults_and_the_first_update_creates_it(path):
    settings = ChannelSettings()
    assert not path.exists()
    settings.update("c.three", muted=True)
    assert set(_channels(path)) == {"c.three", MONITOR_CHANNEL}
    assert _asides(path) == []
