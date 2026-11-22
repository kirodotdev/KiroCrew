"""A ``voice_reply`` change reaches the running Slack gateway's voice state.

``kirocrew config set voice_reply.enabled false`` writes the file. The live
``_vc.global_enabled`` must follow it, or every thread, including one whose
``!voice off`` has just run, still gets an audio attachment. The watcher hands
the applier a change; it re-reads config.json under config.local.json strictly,
and ``!voice global`` saves to the same file, so every surface agrees with it.
"""

from __future__ import annotations

import dataclasses
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.config.live import ConfigChange, ConfigDeferred
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.slack import handler as h

_LONG_ANSWER = "This reply is comfortably longer than the fifty-character voice threshold."


@pytest.fixture
def voice_on(tmp_path, monkeypatch):
    """Live voice state as a gateway that booted with ``enabled: true`` holds it."""
    pristine = h._VoiceConfig()
    for field in dataclasses.fields(h._vc):
        monkeypatch.setattr(h._vc, field.name, getattr(pristine, field.name))
    monkeypatch.setattr(h._vc, "global_enabled", True)
    monkeypatch.setattr(h._vc, "auto_reply_to_voice", True)
    monkeypatch.setattr(h, "config_local_path", lambda: tmp_path / "config.local.json")
    monkeypatch.setattr(h, "_tts_available", lambda **kw: True)
    monkeypatch.setattr(h, "_background_tasks", set())
    spoken: list[str] = []

    async def _capture(slack, channel, reply_ts, text, **kw):
        spoken.append(reply_ts)

    monkeypatch.setattr(h, "_safe_voice_reply", _capture)
    return spoken


def _write(tmp_path, monkeypatch, text: str, local: str | None = None) -> None:
    p = tmp_path / "config.json"
    p.write_text(text)
    monkeypatch.setattr(h, "config_path", lambda: p)
    if local is not None:
        (tmp_path / "config.local.json").write_text(local)


def _change(*paths: str) -> ConfigChange:
    cfg = KiroCrewConfig()
    return ConfigChange(old=cfg, new=cfg, changed=frozenset(paths))


async def _reply_in_thread(session_key: str) -> None:
    await h._reply_by_voice(
        MagicMock(), "D1", "1.0", "U1", session_key, _LONG_ANSWER, _LONG_ANSWER, False
    )
    for task in list(h._background_tasks):
        await task


def _voice_applier():
    from aiohttp import web

    from kiro_crew.dashboard.server import _register_config_watch

    app = web.Application()
    state = SimpleNamespace(
        workflow_service=None, channel_manager=None, set_dynamic_cards_enabled=MagicMock()
    )
    _register_config_watch(app, state, None)
    subs = {s.name: s for s in app["config_watch_subscriptions"]}
    return subs["voice_reply"].callback()


def test_loader_turns_off_a_live_global_voice(tmp_path, monkeypatch, voice_on):
    _write(tmp_path, monkeypatch, json.dumps({"voice_reply": {"enabled": False}}))
    h.load_voice_reply_config(None)
    assert h._vc.global_enabled is False
    assert h._vc.auto_reply_to_voice is False


_BASE_OFF = json.dumps({"voice_reply": {"enabled": False}})
_LOCAL_ON = json.dumps({"voice_reply": {"enabled": True}})


@pytest.mark.asyncio
async def test_overlay_enabled_wins_at_boot_and_on_reload(tmp_path, monkeypatch, voice_on):
    """config.local.json is the operator's overlay; its keys win, as the loader merges."""
    _write(tmp_path, monkeypatch, _BASE_OFF, local=_LOCAL_ON)
    h._vc.global_enabled = False
    h.set_orch_cfg(KiroCrewConfig())
    assert h._vc.global_enabled is True
    h._vc.global_enabled = False
    await _voice_applier()(_change("voice_reply.voice_id"))
    assert h._vc.global_enabled is True


@pytest.mark.asyncio
async def test_config_change_stops_audio_in_a_thread(tmp_path, monkeypatch, voice_on):
    _write(tmp_path, monkeypatch, _BASE_OFF)
    await _voice_applier()(_change("voice_reply.enabled"))
    await _reply_in_thread("slack:1.0")
    assert voice_on == []


@pytest.mark.asyncio
async def test_a_torn_file_keeps_voice_and_defers(tmp_path, monkeypatch, voice_on):
    """A save that leaves the file half-written: nothing changes, and the watcher retries."""
    _write(tmp_path, monkeypatch, '{"voice_reply": {"enabled": fa')
    with pytest.raises(ConfigDeferred) as raised:
        await _voice_applier()(_change("voice_reply.enabled", "agent.model"))
    assert raised.value.paths == {"voice_reply.enabled"}
    assert h._vc.global_enabled is True


@pytest.mark.asyncio
async def test_reload_applies_the_file_it_read_strictly(tmp_path, monkeypatch, voice_on):
    """config.json is read once: a save that truncates it after that read changes nothing."""
    _write(tmp_path, monkeypatch, json.dumps({"voice_reply": {"enabled": True, "voice_id": "Amy"}}))
    real = h.read_merged_config_files
    reads: list[bool] = []

    def _read_once(*paths, strict=False):
        if reads:
            (tmp_path / "config.json").write_text('{"voice_reply": {"ena')
        reads.append(strict)
        return real(*paths, strict=strict)

    monkeypatch.setattr(h, "read_merged_config_files", _read_once)
    h._vc.global_enabled = False
    await _voice_applier()(_change("voice_reply.voice_id"))
    assert h._vc.global_enabled is True
    assert h._vc.default_voice == "Amy"


@pytest.mark.asyncio
async def test_a_broken_overlay_does_not_stop_reloads(tmp_path, monkeypatch, voice_on):
    """The loader degrades an unparseable config.local.json; the reload does too."""
    _write(tmp_path, monkeypatch, _BASE_OFF, local='{"voice_reply": ,}')
    await _voice_applier()(_change("voice_reply.enabled"))
    assert h._vc.global_enabled is False


@pytest.mark.asyncio
async def test_thread_voice_on_still_speaks_with_global_off(tmp_path, monkeypatch, voice_on):
    """Documented precedence: a thread's ``!voice on`` opts that thread in."""
    _write(tmp_path, monkeypatch, _BASE_OFF)
    await _voice_applier()(_change("voice_reply.enabled"))
    monkeypatch.setattr(h._vc, "sessions", {"slack:2.0"})
    await _reply_in_thread("slack:1.0")
    await _reply_in_thread("slack:2.0")
    assert voice_on == ["1.0"]


async def _voice_global(monkeypatch) -> MagicMock:
    monkeypatch.setattr(h, "sel", MagicMock())
    slack = MagicMock(post_message=AsyncMock())
    await h._bang_voice("!voice global", slack, MagicMock(), "D1", "1.0", "1.0", "k", "U1", None)
    return slack


@pytest.mark.asyncio
async def test_voice_global_is_saved_and_survives_a_later_edit(tmp_path, monkeypatch, voice_on):
    """``!voice global`` writes ``voice_reply.enabled``; a voice-only edit keeps it."""
    _write(tmp_path, monkeypatch, _BASE_OFF)
    monkeypatch.setattr(h._vc, "global_enabled", False)
    monkeypatch.setattr(h, "overlay_pins", lambda *key: False)
    await _voice_global(monkeypatch)
    on_disk = json.loads((tmp_path / "config.json").read_text())
    assert on_disk["voice_reply"]["enabled"] is True
    assert h._vc.global_enabled is True

    on_disk["voice_reply"]["voice_id"] = "Joanna"
    (tmp_path / "config.json").write_text(json.dumps(on_disk))
    await _voice_applier()(_change("voice_reply.voice_id"))
    assert h._vc.global_enabled is True


@pytest.mark.asyncio
async def test_voice_global_says_when_the_overlay_owns_enabled(tmp_path, monkeypatch, voice_on):
    """The live flag follows the overlay's value, and the reply names the overlay."""
    _write(tmp_path, monkeypatch, _BASE_OFF, local=_BASE_OFF)
    monkeypatch.setattr(h._vc, "global_enabled", False)
    monkeypatch.setattr(h, "overlay_pins", lambda *key: key == ("voice_reply", "enabled"))
    slack = await _voice_global(monkeypatch)
    assert h._vc.global_enabled is False
    reply = slack.post_message.await_args.args[1]
    assert "OFF" in reply and "config.local.json" in reply
