"""Tests for the ``stt.dictation_panel`` setting.

The dictation panel replaces the thin voice status bar with an animated panel
while recording. It is a display preference, so the contract that matters is:
it defaults to ON, it round-trips through the PUT/GET handler, and it only ever
accepts a real boolean (a truthy string must not silently enable it).


Per-test config isolation comes from the autouse KIROCREW_HOME fixture in
conftest, so these do not take tmp_path themselves.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web

import kiro_crew.dashboard.handlers.core as core
from kiro_crew.config.loader import KiroCrewConfig, config_path


def _req(method: str, body: dict | None = None):
    req = MagicMock(spec=web.Request)
    req.method = method
    if body is not None:
        req.json = AsyncMock(return_value=body)
    return req


@pytest.fixture(autouse=True)
def _stub_probes(monkeypatch):
    monkeypatch.setattr(core, "_stt_prereq_commands", lambda provider: {})
    monkeypatch.setattr(core, "is_available", lambda stt: False)


def test_defaults_to_enabled() -> None:
    """Absent from config.json, the panel is on: it is the standard recording UI."""
    assert KiroCrewConfig.load().stt.dictation_panel is True


def test_explicit_false_is_honoured() -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"stt": {"dictation_panel": False}}), encoding="utf-8")
    assert KiroCrewConfig.load().stt.dictation_panel is False


@pytest.mark.asyncio
async def test_put_then_get_round_trips() -> None:
    resp = await core.api_stt_config(_req("PUT", {"dictation_panel": False}))
    assert resp.status == 200
    assert json.loads(resp.body)["dictation_panel"] is False

    data = json.loads(config_path().read_text(encoding="utf-8"))
    assert data["stt"]["dictation_panel"] is False

    # And back on again.
    resp = await core.api_stt_config(_req("PUT", {"dictation_panel": True}))
    assert json.loads(resp.body)["dictation_panel"] is True


@pytest.mark.asyncio
async def test_get_exposes_the_flag() -> None:
    """The frontend gates the panel on this field, so GET must always carry it —
    a missing key would read as ``undefined`` and silently disable the panel."""
    resp = await core.api_stt_config(_req("GET"))
    assert resp.status == 200
    assert json.loads(resp.body)["dictation_panel"] is True


@pytest.mark.asyncio
async def test_non_boolean_is_rejected() -> None:
    """Only real booleans are accepted. A truthy string must not enable it, and
    must not be persisted as a string the loader would then coerce."""
    await core.api_stt_config(_req("PUT", {"dictation_panel": False}))
    resp = await core.api_stt_config(_req("PUT", {"dictation_panel": "yes"}))
    assert resp.status == 200
    # Unchanged by the bogus write.
    assert json.loads(resp.body)["dictation_panel"] is False
    data = json.loads(config_path().read_text(encoding="utf-8"))
    assert data["stt"]["dictation_panel"] is False


def test_non_bool_in_config_file_falls_back_to_default() -> None:
    """A hand-edited config.json can hold any JSON type. The PUT handler rejects
    non-bools, but nothing validates the file itself — and the frontend gates on
    `dictation_panel !== false`, so a string "false" would read as truthy and
    ENABLE the panel the user meant to switch off. The loader must coerce."""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    for bogus in ("false", "true", 0, 1, None, [], {}):
        path.write_text(json.dumps({"stt": {"dictation_panel": bogus}}), encoding="utf-8")
        loaded = KiroCrewConfig.load().stt.dictation_panel
        assert loaded is True, f"{bogus!r} should fall back to the default, got {loaded!r}"


@pytest.mark.asyncio
async def test_put_does_not_disturb_sibling_stt_fields() -> None:
    """A display-only toggle must not clobber capture settings."""
    await core.api_stt_config(_req("PUT", {"language_code": "fr-FR", "streaming": True}))
    await core.api_stt_config(_req("PUT", {"dictation_panel": False}))
    data = json.loads(config_path().read_text(encoding="utf-8"))
    assert data["stt"]["language_code"] == "fr-FR"
    assert data["stt"]["streaming"] is True
    assert data["stt"]["dictation_panel"] is False
