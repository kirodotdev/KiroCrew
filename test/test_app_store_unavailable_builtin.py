"""A built-in app skipped at boot is reported to the App Store as unavailable, with why.

``dashboard.routes.system.register`` skips a built-in app whose import raises
``OSError``. The skip is recorded on the application, and ``GET /api/apps`` and
``GET /api/apps/{name}`` stamp ``unavailable: {"reason": ...}`` on that app's
built-in record, so the dashboard does not present it as a working app.
"""

from __future__ import annotations

import importlib
import json
from importlib import resources
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew.apps import routes_listing
from kiro_crew.apps.builtin_unavailable import (
    builtin_app_name,
    mark_unavailable,
    record_unavailable_builtin,
    unavailable_builtins,
)
from kiro_crew.apps.builtins import BUILTIN_NAMES
from kiro_crew.dashboard.routes import system


def _app() -> web.Application:
    app = web.Application()
    app["state"] = SimpleNamespace(owner_id="owner", broadcast_ws=lambda *a, **k: None)
    return app


def test_boot_records_the_skipped_builtin_and_its_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    failing = BUILTIN_NAMES[0]
    real_import = importlib.import_module

    def _import(name: str, *args, **kwargs):
        if name == f"kiro_crew.apps.builtins.{failing}":
            raise PermissionError(13, "Permission denied")
        return real_import(name, *args, **kwargs)

    baseline = _app()
    system.register(baseline)
    assert unavailable_builtins(baseline) == {}

    monkeypatch.setattr(system.importlib, "import_module", _import)
    app = _app()
    system.register(app)
    assert unavailable_builtins(app) == {
        builtin_app_name(failing): "PermissionError: [Errno 13] Permission denied"
    }


def test_the_skip_is_keyed_by_the_installed_app_name() -> None:
    # A package spelled with ``_`` ships a manifest spelled with ``-``; the record
    # ``GET /api/apps`` carries uses the manifest name.
    app = _app()
    record_unavailable_builtin(app, "code_review_sage", PermissionError(13, "Permission denied"))
    stamped = mark_unavailable(
        {"name": "code-review-sage", "origin": "builtin"}, unavailable_builtins(app)
    )
    assert stamped["unavailable"]["reason"].startswith("PermissionError")


@pytest.mark.parametrize("module_name", BUILTIN_NAMES)
def test_every_shipped_builtin_follows_the_name_rule(module_name: str) -> None:
    manifest = resources.files(f"kiro_crew.apps.builtins.{module_name}").joinpath("app.json")
    assert json.loads(manifest.read_text(encoding="utf-8"))["name"] == builtin_app_name(module_name)


def test_only_the_skipped_builtin_record_is_stamped() -> None:
    skipped = {"mochi": "PermissionError: [Errno 13] Permission denied"}

    builtin = mark_unavailable({"name": "mochi", "origin": "builtin"}, skipped)
    assert builtin["unavailable"] == {"reason": "PermissionError: [Errno 13] Permission denied"}

    # An external app that happens to share the name is a different app.
    external = mark_unavailable({"name": "mochi", "origin": "registry"}, skipped)
    assert "unavailable" not in external

    loaded = mark_unavailable({"name": "secretary", "origin": "builtin"}, skipped)
    assert "unavailable" not in loaded


def test_a_record_written_before_any_skip_reads_as_none() -> None:
    assert unavailable_builtins(web.Application()) == {}


@pytest.mark.asyncio
async def test_the_list_and_detail_routes_carry_the_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _app()
    record_unavailable_builtin(app, "mochi", PermissionError(13, "Permission denied"))
    records = [
        {"name": "mochi", "origin": "builtin", "enabled": True},
        {"name": "secretary", "origin": "builtin", "enabled": True},
    ]
    monkeypatch.setattr(
        routes_listing, "_listed_apps_with_trust", lambda: [dict(r) for r in records]
    )
    monkeypatch.setattr(routes_listing, "list_app_processes", lambda: [])
    monkeypatch.setattr(routes_listing, "get_all_hook_health", lambda: {})
    monkeypatch.setattr(routes_listing, "get_app", lambda name: dict(records[0]))
    monkeypatch.setattr(routes_listing, "_installed_app_detail", lambda name, info: info)

    listed = await routes_listing.handle_list_apps(make_mocked_request("GET", "/api/apps", app=app))
    by_name = {r["name"]: r for r in json.loads(listed.body)}
    assert by_name["mochi"]["unavailable"] == {
        "reason": "PermissionError: [Errno 13] Permission denied"
    }
    assert "unavailable" not in by_name["secretary"]

    detail = await routes_listing.handle_get_app(
        make_mocked_request("GET", "/api/apps/mochi", app=app, match_info={"name": "mochi"})
    )
    assert json.loads(detail.body)["unavailable"] == {
        "reason": "PermissionError: [Errno 13] Permission denied"
    }
