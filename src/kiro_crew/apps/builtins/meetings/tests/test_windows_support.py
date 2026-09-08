"""Windows-support contract for the Meetings builtin.

Two halves, failing for different reasons:

* The **manifest** half pins ``platform.os``. The app gate is
  ``supports_platform(sys.platform)``, so a narrower list is not a documentation
  nit — it is what stops the app from being enabled at all. It shipped as
  ``["macos", "linux"]``, which predated the audio-import Windows hardening
  rather than expressing any real POSIX dependency: the app spawns no
  subprocess, imports no POSIX-only module, writes every file through
  ``atomic_write``, serialises with ``threading`` locks rather than ``fcntl``,
  and already branches on ``platform_compat.IS_WINDOWS`` where it matters.
* The **capability** half pins the one thing that genuinely differs on Windows:
  importing an existing recording needs pinned traversal (``dir_fd`` with
  ``O_NOFOLLOW``) and is refused without it. That refusal is deliberate and
  these tests do not challenge it — what they guard is that it is ANNOUNCED
  through ``GET .../config``, instead of being discovered as a 501 after the
  user has already chosen a file.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew.apps.builtins.meetings.backend.routes import settings as settings_routes

# .../meetings/tests/test_windows_support.py -> parents[1] is the app root.
APP_ROOT = Path(__file__).resolve().parents[1]
APP_JSON = APP_ROOT / "app.json"

DECLARED_OS = ["macos", "linux", "windows"]


def _manifest() -> dict:
    return json.loads(APP_JSON.read_text(encoding="utf-8"))


# --- manifest platform declaration ---


def test_manifest_declares_every_platform_the_app_runs_on():
    """Pinned because both narrower answers misinform.

    ``["macos", "linux"]`` reads as "does not run on Windows" and makes the
    enable gate refuse; omitting the block falls back to the implicit default,
    which silently drops Windows the same way.
    """
    assert _manifest()["platform"]["os"] == DECLARED_OS


def test_declared_platforms_all_resolve_to_a_real_sys_platform():
    """Every declared name must map to a ``sys.platform`` value.

    An unmapped name is accepted into the list and then never matches, so a
    declaration can claim a platform the gate rejects.
    """
    from kiro_crew.apps.manifest import PlatformConfig

    cfg = PlatformConfig(os=_manifest()["platform"]["os"])
    for sys_platform in ("darwin", "linux", "win32"):
        assert cfg.supports_platform(sys_platform), sys_platform


# --- audio-import capability is published, not discovered by failing ---


def test_capability_is_supported_where_the_platform_can_pin(monkeypatch):
    monkeypatch.setattr(settings_routes, "supports_pinned_walk", lambda: True)
    assert settings_routes._audio_import_capability() == {
        "supported": True,
        "code": "",
        "reason": "",
    }


def test_capability_refuses_with_a_readable_reason_where_it_cannot_pin(monkeypatch):
    """A bare ``False`` would make the disabled Import control read as a bug.

    The payload has to carry why the platform cannot do it and what still works,
    because that text is the only explanation the user ever sees.
    """
    monkeypatch.setattr(settings_routes, "supports_pinned_walk", lambda: False)
    cap = settings_routes._audio_import_capability()
    assert cap["supported"] is False
    assert cap["code"] == settings_routes.IMPORT_UNSUPPORTED_CODE
    assert "dir_fd" in cap["reason"]
    assert "live transcription" in cap["reason"].lower()


def test_published_code_matches_the_code_the_import_route_raises():
    """The published code and the route's own refusal code must not drift.

    The frontend keys off the published one; if the route ever renames its
    ``code`` the UI would stop recognising the refusal it is meant to pre-empt.
    """
    source = (APP_ROOT / "backend" / "routes" / "audio_import.py").read_text(encoding="utf-8")
    assert f'code="{settings_routes.IMPORT_UNSUPPORTED_CODE}"' in source


@pytest.mark.asyncio
async def test_config_endpoint_publishes_the_import_capability(monkeypatch, tmp_path):
    """The capability must reach the wire, not just the helper.

    Asserted through the handler because the frontend reads it from this one
    response; a helper-only test would pass with the field never serialised.
    """
    monkeypatch.setattr(settings_routes.store, "read_config", lambda root: {"meeting_agents": []})
    monkeypatch.setattr(settings_routes, "supports_pinned_walk", lambda: False)

    app = web.Application()
    app["_meetings_data_root"] = tmp_path
    request = make_mocked_request("GET", "/api/apps/meetings/config", app=app)

    response = await settings_routes.handle_get_config(request)
    assert isinstance(response.body, bytes)
    payload = json.loads(response.body)

    assert payload["audio_import"]["supported"] is False
    assert payload["audio_import"]["code"] == settings_routes.IMPORT_UNSUPPORTED_CODE
    assert payload["audio_import"]["reason"]


@pytest.mark.asyncio
async def test_config_endpoint_reports_support_where_pinning_works(monkeypatch, tmp_path):
    monkeypatch.setattr(settings_routes.store, "read_config", lambda root: {"meeting_agents": []})
    monkeypatch.setattr(settings_routes, "supports_pinned_walk", lambda: True)

    app = web.Application()
    app["_meetings_data_root"] = tmp_path
    request = make_mocked_request("GET", "/api/apps/meetings/config", app=app)

    response = await settings_routes.handle_get_config(request)
    assert isinstance(response.body, bytes)
    payload = json.loads(response.body)
    assert payload["audio_import"] == {"supported": True, "code": "", "reason": ""}
