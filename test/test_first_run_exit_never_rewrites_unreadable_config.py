"""Leaving first run without saving must not let a LATER write replace config.json.

"Continue without saving" (AgentImportFlow) exists for the case where
``config.json`` does not parse. The loader reads that file as defaults, so the
flow's own ``PUT /api/onboarding/import/state`` refuses, and the button takes the
exit locally. What follows that exit is the rest of first run, and each step
persists something: the Privacy chapter's ``privacy_acked``, the tour's theme,
colour and language, ``onboarded`` / ``import_onboarded`` /
``crewmates_onboarded``, and the tour's "about you" ``dashboard.user_*`` fields.

Every one of those must refuse WITHOUT writing while the file is unreadable,
because a write built from the defaults snapshot would replace every setting the
user has. This drives the real handlers against a real torn ``config.json`` and
compares the bytes afterwards. The theme route's refusal comes from
``KiroCrewConfig.save``, which fails closed on such a file.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web

from kiro_crew.config.loader import config_path
from kiro_crew.dashboard.handlers import core as core_mod
from kiro_crew.dashboard.handlers import onboarding_import as onboarding_mod

_UNREADABLE = b'{"dashboard": {"theme_mode": "light", "onboarded": tr'


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    assert config_path().parent == tmp_path
    config_path().write_bytes(_UNREADABLE)
    return tmp_path


def _owner_request(method: str, body: dict) -> MagicMock:
    request = MagicMock(spec=web.Request)
    state = MagicMock()
    state.owner_id = ""
    request.app = {"state": state}
    claims = {"user": "local-app", "app": ""}
    request.get = lambda key, default=None: claims.get(key, default)
    request.__contains__.side_effect = lambda key: key in claims
    request.__getitem__.side_effect = lambda key: claims[key]
    request.method = method
    request.json = AsyncMock(return_value=body)
    return request


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"privacy_acked": True},
        {"onboarded": True},
        {"import_onboarded": True},
        {"crewmates_onboarded": True},
        {"mode": "dark"},
        {"color": "monokai"},
        {"language": "ja"},
    ],
    ids=lambda body: next(iter(body)),
)
async def test_every_first_run_flag_write_refuses(home: Path, body: dict) -> None:
    resp = await core_mod.api_theme_config(_owner_request("PUT", body))

    assert resp.status >= 400
    assert config_path().read_bytes() == _UNREADABLE


@pytest.mark.asyncio
async def test_the_import_state_write_refuses(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(onboarding_mod, "_sel", lambda: MagicMock())
    request = _owner_request("PUT", {"completed": True})

    resp = await onboarding_mod.api_onboarding_import_state(request)

    assert resp.status == 500
    assert config_path().read_bytes() == _UNREADABLE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("dashboard.user_role", "engineer"),
        ("dashboard.user_technical_level", "expert"),
    ],
)
async def test_the_tour_profile_writes_refuse(
    home: Path, monkeypatch: pytest.MonkeyPatch, path: str, value: str
) -> None:
    monkeypatch.setattr(core_mod, "_sel", lambda: MagicMock())

    resp = await core_mod.api_kirocrew_config_patch(
        _owner_request("PATCH", {"path": path, "value": value})
    )

    assert resp.status >= 400
    assert config_path().read_bytes() == _UNREADABLE
