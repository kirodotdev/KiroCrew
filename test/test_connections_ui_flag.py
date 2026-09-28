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


def test_flag_absent_defaults_the_gallery_on(tmp_path, monkeypatch):
    """Launch posture: a config that never mentions the flag ships the gallery.

    The N8 flip is only real if the BACKEND defaults the field on: the loader
    materializes ``connections_ui`` into every masked GET, so a frontend that
    tolerates an absent key still reads a confirmed ``false`` from a default-off
    backend and the launch never fires on a real install.
    """
    _point_loader_at(tmp_path, monkeypatch, {"agent": {"provider": "acp"}})
    masked = _masked(KiroCrewConfig.load())

    assert masked.get(FLAG) is True
    assert _frontend_says_enabled(masked)


def test_flag_set_false_leaves_the_gallery_off(tmp_path, monkeypatch):
    """The deliberate opt-out. Post-launch, a bare stored ``false`` is
    indistinguishable from pre-launch materialized noise, so the honoured
    opt-out is ``false`` WITH the migration marker present — the state every
    install reaches after its first post-launch boot."""
    _point_loader_at(tmp_path, monkeypatch, {FLAG: False})
    (tmp_path / L.CONNECTIONS_UI_MIGRATION_MARKER).write_text("{}", encoding="utf-8")
    masked = _masked(KiroCrewConfig.load())

    assert masked.get(FLAG) is False
    assert not _frontend_says_enabled(masked)


def test_a_non_bool_value_falls_back_to_the_default(tmp_path, monkeypatch):
    """``"true"`` is not ``true``: an unparseable value falls back to the default.

    Pre-launch this pinned fail-CLOSED, because the flag revealed a
    held-for-release surface. Default-on inverts the safety direction: the flag
    is now an opt-OUT, and the only honest reading of a value Kiro Crew cannot
    parse is the same fallback every other ``_safe_bool`` field takes — the
    default — never a guess at operator intent. An explicit boolean ``false``
    remains the one way to hide the gallery (pinned separately below), and the
    frontend still receives a real boolean either way.
    """
    _point_loader_at(tmp_path, monkeypatch, {FLAG: "true"})
    cfg = KiroCrewConfig.load()

    assert cfg.connections_ui is True
    assert _frontend_says_enabled(_masked(cfg))


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


# ---------------------------------------------------------------------------
# The launch migration: pre-launch builds materialized ``connections_ui: false``
# into every config they saved (the key was the opt-in gate then, so a stored
# false was the default's noise, never a choice). Post-launch that same byte
# pattern is the deliberate opt-out. The migration strips exactly the noise,
# once: on the first load that finds no marker file, a stored ``false`` is
# removed (the launch default then applies) and the marker is recorded; every
# later load honours ``false`` as the opt-out it now is. ``true`` — the only
# deliberate pre-launch act — is never touched.
# ---------------------------------------------------------------------------


def _marker_path(tmp_path):
    return tmp_path / L.CONNECTIONS_UI_MIGRATION_MARKER


def test_materialized_false_is_stripped_once_and_the_gallery_turns_on(tmp_path, monkeypatch):
    """The upgrade trap: pre-launch noise must not read as an opt-out."""
    _point_loader_at(tmp_path, monkeypatch, {FLAG: False, "auto_update": True})
    cfg = KiroCrewConfig.load()

    assert cfg.connections_ui is True, "stale materialized false still wins the load"
    on_disk = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert FLAG not in on_disk, "the stale key must be stripped from the document"
    assert on_disk.get("auto_update") is True, "the delta must not touch other keys"
    assert _marker_path(tmp_path).exists(), "the one-shot boundary must be recorded"


def test_false_after_the_marker_is_a_deliberate_opt_out(tmp_path, monkeypatch):
    """Post-migration, explicit false is the honoured opt-out — forever."""
    _point_loader_at(tmp_path, monkeypatch, {FLAG: False})
    _marker_path(tmp_path).write_text("{}", encoding="utf-8")
    cfg = KiroCrewConfig.load()

    assert cfg.connections_ui is False
    on_disk = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert on_disk.get(FLAG) is False, "a marked config is never rewritten"


def test_true_is_never_touched_and_still_records_the_marker(tmp_path, monkeypatch):
    """Pre-launch true was the one deliberate act; it survives, marker lands."""
    _point_loader_at(tmp_path, monkeypatch, {FLAG: True})
    cfg = KiroCrewConfig.load()

    assert cfg.connections_ui is True
    on_disk = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert on_disk.get(FLAG) is True
    assert _marker_path(tmp_path).exists()


def test_absent_key_records_the_marker_without_touching_the_flag(tmp_path, monkeypatch):
    """Nothing to migrate: no flag key materializes, only the marker is written."""
    _point_loader_at(tmp_path, monkeypatch, {"auto_update": True})
    cfg = KiroCrewConfig.load()

    assert cfg.connections_ui is True
    # Other one-shot migrations (agents seeding) may rewrite the document; the
    # contract HERE is only that the flag key is not invented on disk.
    on_disk = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert FLAG not in on_disk
    assert _marker_path(tmp_path).exists()


def test_second_load_after_migration_is_a_no_op(tmp_path, monkeypatch):
    """Idempotency: the marker makes the migration one-shot."""
    _point_loader_at(tmp_path, monkeypatch, {FLAG: False})
    KiroCrewConfig.load()
    migrated = (tmp_path / "config.json").read_text(encoding="utf-8")
    marker_stat = _marker_path(tmp_path).stat().st_mtime_ns

    cfg = KiroCrewConfig.load()
    assert cfg.connections_ui is True
    assert (tmp_path / "config.json").read_text(encoding="utf-8") == migrated
    assert _marker_path(tmp_path).stat().st_mtime_ns == marker_stat


def test_a_deferred_strip_defers_the_marker_too(tmp_path, monkeypatch):
    """Lock contention: ``_persist_config_migration`` swallows a contended lock
    and returns False — the strip is deferred to the next load. The marker MUST
    defer with it: marker-without-strip freezes the stale ``false`` as a
    deliberate opt-out forever (the next load sees marker present and honours
    it). Pins that the marker write is gated on the persist outcome."""
    _point_loader_at(tmp_path, monkeypatch, {FLAG: False})
    calls: list[frozenset] = []

    def contended(path, pending, **kwargs):
        calls.append(pending)
        return False  # what a BlockingIOError-swallowing persist returns

    monkeypatch.setattr(L, "_persist_config_migration", contended)
    cfg = KiroCrewConfig.load()

    # This boot still serves the launch default in memory...
    assert cfg.connections_ui is True
    # ...but on disk NOTHING moved: false still present, marker absent,
    # so the next (uncontended) load retries the whole migration.
    on_disk = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert on_disk.get(FLAG) is False
    assert not _marker_path(tmp_path).exists()
    assert any(L.MIGRATE_CONNECTIONS_UI in p for p in calls)
