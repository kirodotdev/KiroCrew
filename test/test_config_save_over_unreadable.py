"""A whole-document save must never replace a config.json the load could not read.

``KiroCrewConfig.load`` answers an unparseable ``config.json`` with DEFAULTS (it
warns and marks the file degraded), and ``save`` publishes the instance it is
called on. So "load, change one display field, save" over such a file replaced
every setting the user had with defaults, silently. ``PUT /api/config/theme``
does exactly that on any theme, language or onboarding-flag change, and first
run reaches it on the ordinary path: an unreadable config reads every onboarding
flag false, the user passes the chapters, and the next flag write wiped the file.

The locked read-modify-write (``update_config_locked``) already fails closed on
the same file. These tests pin ``save`` to the same rule, and the theme route to
answer it with a coded refusal instead of a 500 trace.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web

from kiro_crew.config.loader import ConfigReadError, KiroCrewConfig, config_path
from kiro_crew.dashboard.handlers import core as core_mod

# Cut off mid-document, as a torn hand edit leaves it.
_UNREADABLE = b'{"dashboard": {"theme_mode": "light", "onboarded": tr'


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    assert config_path().parent == tmp_path
    return tmp_path


def _owner_put(body: dict) -> MagicMock:
    request = MagicMock(spec=web.Request)
    state = MagicMock()
    state.owner_id = ""
    request.app = {"state": state}
    claims = {"user": "local-app", "app": ""}
    request.get = lambda key, default=None: claims.get(key, default)
    request.__contains__.side_effect = lambda key: key in claims
    request.__getitem__.side_effect = lambda key: claims[key]
    request.method = "PUT"
    request.json = AsyncMock(return_value=body)
    return request


def test_save_refuses_to_replace_an_unreadable_config(home: Path) -> None:
    config_path().write_bytes(_UNREADABLE)
    cfg = KiroCrewConfig.load()
    # The load really did fall back to defaults: this is the snapshot save holds.
    assert cfg.dashboard.onboarded is False

    cfg.dashboard.theme_mode = "dark"
    with pytest.raises(ConfigReadError):
        cfg.save()

    assert config_path().read_bytes() == _UNREADABLE


def test_save_refuses_a_defaults_snapshot_even_after_the_file_is_repaired(home: Path) -> None:
    # The operator fixes config.json while a dashboard request is between its
    # load and its save. The file now parses, but this snapshot still holds
    # defaults, so publishing it would replace the repaired settings.
    config_path().write_bytes(_UNREADABLE)
    cfg = KiroCrewConfig.load()
    repaired = json.dumps({"dashboard": {"theme_mode": "light", "onboarded": True}}).encode()
    config_path().write_bytes(repaired)

    cfg.dashboard.theme_mode = "dark"
    with pytest.raises(ConfigReadError):
        cfg.save()

    assert config_path().read_bytes() == repaired


def test_save_still_creates_a_missing_config(home: Path) -> None:
    assert not config_path().exists()

    KiroCrewConfig().save()

    assert isinstance(json.loads(config_path().read_text(encoding="utf-8")), dict)


def test_save_still_rewrites_a_readable_config(home: Path) -> None:
    config_path().write_text(json.dumps({"dashboard": {"theme_mode": "light"}}), encoding="utf-8")
    cfg = KiroCrewConfig.load()
    cfg.dashboard.theme_mode = "dark"

    cfg.save()

    assert (
        json.loads(config_path().read_text(encoding="utf-8"))["dashboard"]["theme_mode"] == "dark"
    )


@pytest.mark.asyncio
async def test_theme_put_over_an_unreadable_config_refuses_and_keeps_the_file(home: Path) -> None:
    config_path().write_bytes(_UNREADABLE)

    resp = await core_mod.api_theme_config(_owner_put({"privacy_acked": True, "mode": "dark"}))

    assert resp.status == 500
    assert json.loads(resp.body) == {
        "error": "failed to read config file",
        "code": "config_unreadable",
    }
    assert config_path().read_bytes() == _UNREADABLE
