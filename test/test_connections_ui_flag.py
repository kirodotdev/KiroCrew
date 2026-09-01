"""``connections_ui`` must survive load() -> masked GET -> frontend predicate.

The Connections gallery is held for a later release and is reachable only when
``connections_ui: true`` is set as a TOP-LEVEL key in the running instance's
``config.json``. The frontend reads that flag live off ``GET
/api/config/kirocrew`` (``website/src/hooks/useConnectionsUi.ts``:
``connectionsUiEnabled`` requires the value to be exactly ``true``), so the
whole feature turns on the key reaching the browser in that response body.

Nothing but a real, schema-known config value gets there. An unmodelled
top-level key is captured into ``KiroCrewConfig._extra_sections`` at load and
then dropped wholesale by ``_masked_config_dict`` — a deliberate guard, because
an edition-contributed section is absent from the schema and the sensitivity
walk cannot know which of its values are secrets. These tests pin the flag on
the modelled side of that line while keeping the guard itself intact: a
genuinely unknown key must still never reach the browser.
"""

from __future__ import annotations

import json

import pytest

from kiro_crew.config import loader as L
from kiro_crew.config.loader import KiroCrewConfig

# The one spelling the frontend, the docs and existing user configs all use.
FLAG = "connections_ui"


def _point_loader_at(tmp_path, monkeypatch, data: dict) -> None:
    """Write *data* as the instance config and aim the loader at it."""
    cfgp = tmp_path / "config.json"
    cfgp.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(L, "config_path", lambda: cfgp)
    monkeypatch.setattr(L, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(L, "config_local_path", lambda: tmp_path / "config.local.json")


def _masked(cfg: KiroCrewConfig) -> dict:
    from kiro_crew.dashboard.handlers.core import _masked_config_dict

    return _masked_config_dict(cfg)


def _frontend_says_enabled(masked: dict) -> bool:
    """Mirror of ``connectionsUiEnabled`` — strict ``=== true``, nothing else."""
    return masked.get(FLAG) is True


def test_flag_set_true_reaches_the_masked_get(tmp_path, monkeypatch):
    """The launch blocker: ``true`` on disk must arrive in the browser's copy."""
    _point_loader_at(tmp_path, monkeypatch, {FLAG: True})
    cfg = KiroCrewConfig.load()

    assert cfg.connections_ui is True
    # Modelled, therefore NOT swept up as an unknown section...
    assert FLAG not in cfg._extra_sections
    # ...and therefore present in the browser-facing view.
    assert _frontend_says_enabled(_masked(cfg))


def test_flag_absent_leaves_the_gallery_off(tmp_path, monkeypatch):
    """Default posture: a config that never mentions the flag stays closed."""
    _point_loader_at(tmp_path, monkeypatch, {"agent": {"provider": "acp"}})
    masked = _masked(KiroCrewConfig.load())

    assert masked.get(FLAG) is False
    assert not _frontend_says_enabled(masked)


def test_flag_set_false_leaves_the_gallery_off(tmp_path, monkeypatch):
    _point_loader_at(tmp_path, monkeypatch, {FLAG: False})
    masked = _masked(KiroCrewConfig.load())

    assert masked.get(FLAG) is False
    assert not _frontend_says_enabled(masked)


def test_a_non_bool_value_fails_closed(tmp_path, monkeypatch):
    """``"true"`` is not ``true``: an unparseable value must not open the gate.

    Same posture as ``computer_use.cursor_motion`` — for a flag that reveals a
    held-for-release surface, a value Kiro Crew cannot read means "off", never
    the reverse. Coercing the string would also hand the frontend a value its
    strict ``=== true`` check rejects anyway, so the two would disagree about
    what the operator configured.
    """
    _point_loader_at(tmp_path, monkeypatch, {FLAG: "true"})
    cfg = KiroCrewConfig.load()

    assert cfg.connections_ui is False
    assert not _frontend_says_enabled(_masked(cfg))


def test_flag_survives_a_config_write(tmp_path, monkeypatch):
    """load() -> save() must not destroy the operator's opt-in.

    A key the core does not model would be re-emitted from
    ``_extra_sections``; a key it models must be emitted from its own field.
    Either way the operator's ``true`` has to still be on disk afterwards, and
    still reach the browser on the next read.
    """
    _point_loader_at(tmp_path, monkeypatch, {FLAG: True, "timezone": "UTC"})
    KiroCrewConfig.load().save()

    written = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert written[FLAG] is True

    assert _frontend_says_enabled(_masked(KiroCrewConfig.load()))


def test_flag_is_schema_known_so_load_does_not_warn_about_it():
    """Registered in the schema, so validation stops calling it unrecognized.

    ``validation.validate_config`` derives its known top-level keys from
    ``SCHEMA_REGISTRY``, so an unmodelled flag makes every load of a
    Connections-enabled config log "unrecognized top-level keys:
    connections_ui" — the same missing-citizenship as the stripped GET.
    """
    from kiro_crew.config.schema import SCHEMA_REGISTRY

    top_level = {e.path for e in SCHEMA_REGISTRY if "." not in e.path}
    assert FLAG in top_level


def test_flag_set_in_the_local_overlay_also_reaches_the_browser(tmp_path, monkeypatch):
    """``config.local.json`` is the documented survives-upgrades place to set it.

    The overlay is deep-merged at load, so the flag has to work from there too —
    and ``save()`` must keep it overlay-owned rather than copying it into the
    base file.
    """
    cfgp = tmp_path / "config.json"
    cfgp.write_text(json.dumps({"agent": {"provider": "acp"}}), encoding="utf-8")
    localp = tmp_path / "config.local.json"
    localp.write_text(json.dumps({FLAG: True}), encoding="utf-8")
    monkeypatch.setattr(L, "config_path", lambda: cfgp)
    monkeypatch.setattr(L, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(L, "config_local_path", lambda: localp)

    cfg = KiroCrewConfig.load()
    assert _frontend_says_enabled(_masked(cfg))

    cfg.save()
    base = json.loads(cfgp.read_text(encoding="utf-8"))
    assert FLAG not in base  # overlay-owned, not leaked into config.json
    assert _frontend_says_enabled(_masked(KiroCrewConfig.load()))


def test_the_unknown_extras_guard_is_not_weakened(tmp_path, monkeypatch):
    """Making ONE key known must not open the browser view to unknown keys.

    The strip exists because an unmodelled value can be a secret and the
    schema-driven sensitivity walk cannot see it. That reasoning covers a
    top-level scalar exactly as much as a section, so both must still be gone
    from the masked response.
    """
    _point_loader_at(
        tmp_path,
        monkeypatch,
        {
            FLAG: True,
            "amazon": {"api_token": "SECRET-section-value"},
            "some_vendor_token": "SECRET-scalar-value",
        },
    )
    cfg = KiroCrewConfig.load()
    masked = _masked(cfg)

    assert _frontend_says_enabled(masked)
    assert "amazon" not in masked
    assert "some_vendor_token" not in masked
    body = json.dumps(masked)
    assert "SECRET-section-value" not in body
    assert "SECRET-scalar-value" not in body
    # The save() path still carries them — only the browser-facing view drops them.
    assert cfg.to_dict()["some_vendor_token"] == "SECRET-scalar-value"


@pytest.mark.asyncio
async def test_the_wire_response_carries_the_flag(tmp_path, monkeypatch):
    """End to end over HTTP, because the wire is what the hook actually reads.

    ``_masked_config_dict`` is one step of the GET; this drives the real route
    so the assertion covers the whole path the frontend depends on.
    """
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from kiro_crew.dashboard.handlers import core as core_mod

    _point_loader_at(tmp_path, monkeypatch, {FLAG: True})

    app = web.Application()
    app.router.add_route("*", "/api/config/kirocrew", core_mod.api_kirocrew_config)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/config/kirocrew")
        assert resp.status == 200
        assert _frontend_says_enabled(await resp.json())
