"""Tests for the ``stt.dictation_note`` setting.

When it is on, the dashboard composer ends a message that holds dictated words
with one note telling the agent the text was dictated. The note itself is added
by the frontend send paths (``website/src/chat-core/composer/dictationProvenance.ts``);
the gateway's part is the setting: it defaults to OFF so existing behaviour is
unchanged, it round-trips through the STT config PUT/GET the Voice panel uses,
and it only ever accepts a real boolean.

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
def _stub_probes(_floor_monkeypatch):
    _floor_monkeypatch.setattr(core, "_stt_prereq_commands", lambda provider: {})
    _floor_monkeypatch.setattr(
        core, "availability_detail", lambda stt: core.stt.Availability(False)
    )


def test_defaults_to_off() -> None:
    """Absent from config.json, nothing is added to anyone's messages."""
    assert KiroCrewConfig.load().stt.dictation_note is False


def test_explicit_true_is_honoured() -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"stt": {"dictation_note": True}}), encoding="utf-8")
    assert KiroCrewConfig.load().stt.dictation_note is True


def test_non_bool_in_config_file_falls_back_to_off() -> None:
    """The frontend adds the note only on a literal ``true``, and the loader must
    agree: a hand-edited string ``"true"`` is not a decision to turn it on."""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    for bogus in ("true", "false", 1, 0, None, [], {}):
        path.write_text(json.dumps({"stt": {"dictation_note": bogus}}), encoding="utf-8")
        loaded = KiroCrewConfig.load().stt.dictation_note
        assert loaded is False, f"{bogus!r} should fall back to the default, got {loaded!r}"


@pytest.mark.asyncio
async def test_get_exposes_the_flag() -> None:
    """The composer reads this field from the same response, so GET always carries it."""
    resp = await core.api_stt_config(_req("GET"))
    assert resp.status == 200
    assert json.loads(resp.body)["dictation_note"] is False


@pytest.mark.asyncio
async def test_put_then_get_round_trips() -> None:
    resp = await core.api_stt_config(_req("PUT", {"dictation_note": True}))
    assert resp.status == 200
    assert json.loads(resp.body)["dictation_note"] is True
    data = json.loads(config_path().read_text(encoding="utf-8"))
    assert data["stt"]["dictation_note"] is True

    resp = await core.api_stt_config(_req("PUT", {"dictation_note": False}))
    assert json.loads(resp.body)["dictation_note"] is False
    assert KiroCrewConfig.load().stt.dictation_note is False


@pytest.mark.asyncio
async def test_non_boolean_put_is_ignored() -> None:
    """A truthy string must not turn it on, nor be stored for the loader to coerce."""
    await core.api_stt_config(_req("PUT", {"dictation_note": False}))
    resp = await core.api_stt_config(_req("PUT", {"dictation_note": "yes"}))
    assert resp.status == 200
    assert json.loads(resp.body)["dictation_note"] is False
    data = json.loads(config_path().read_text(encoding="utf-8"))
    assert data["stt"]["dictation_note"] is False


@pytest.mark.asyncio
async def test_put_leaves_sibling_stt_fields_alone() -> None:
    await core.api_stt_config(_req("PUT", {"language_code": "fr-FR", "dictation_panel": False}))
    await core.api_stt_config(_req("PUT", {"dictation_note": True}))
    data = json.loads(config_path().read_text(encoding="utf-8"))
    assert data["stt"]["language_code"] == "fr-FR"
    assert data["stt"]["dictation_panel"] is False
    assert data["stt"]["dictation_note"] is True
