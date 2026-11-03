"""The ``/api/apps`` adoption rows the dashboard renders, produced by the real handlers.

``website/src/test/fixtures/adoptionApiRows.json`` holds what ``GET /api/apps/{name}``
and ``GET /api/apps`` answer for a pinned former built-in in each adoption state. The
website test ``adoptionApiRows.test.tsx`` renders the app detail notice and the
legacy route from those same bytes, so a renamed or reshaped field fails one side or
the other instead of passing both against hand-typed samples. This test is the
producer half: it rebuilds every row through the handlers and compares.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import make_mocked_request
from builtin_adoption_helpers import (
    NAME,
    PACKAGE,
    REPOSITORY,
    install_pins,
    pin_row,
    repository_manifest,
    shipped_manifest,
    write_builtin_record,
    write_tree,
)

from kiro_crew.apps import builtin_adoption_pins as pins_mod
from kiro_crew.apps import execution as exec_mod
from kiro_crew.apps import manager, routes_listing
from kiro_crew.apps.builtin_adoption_checks import ALL_REASON_CODES
from kiro_crew.apps.builtin_adoption_state import AdoptionLedger, write_ledger

_WEBSITE_SRC = Path(__file__).resolve().parents[1] / "website" / "src"
_FIXTURE = _WEBSITE_SRC / "test" / "fixtures" / "adoptionApiRows.json"
COMMIT = "a" * 40
# A glyph the dashboard's rail registers, so the consumer half sees it kept.
_LEGACY_NAV = {"route": f"/{NAME}", "icon": "ShoppingBag"}


@pytest.fixture
def wheel(tmp_path, monkeypatch):
    """The fixture app shipped in a wheel package and pinned, with no record yet."""
    root = tmp_path / "wheel"
    write_tree(root / PACKAGE, shipped_manifest())
    monkeypatch.setattr(exec_mod, "_builtin_manifest_sources", lambda: (root.resolve(),))
    install_pins(
        monkeypatch,
        tmp_path / "pins.json",
        [pin_row(COMMIT, "b" * 64, "c" * 64, legacyNav=_LEGACY_NAV)],
    )
    yield
    pins_mod.load_adoption_pins.cache_clear()
    manager.invalidate_orphan_cache()


def _adopted_record(**manifest_overrides: Any) -> None:
    """The record a successful APPLY leaves: the repository copy, data kept."""
    write_builtin_record()
    record = manager._read_installed(NAME)
    assert record is not None
    record.source, record.origin, record.lifecycle = f"registry:{NAME}", "registry", "gateway"
    record.sourceUrl, record.sourceCommit = REPOSITORY, COMMIT
    manifest = repository_manifest(**manifest_overrides)
    record.version = manifest["version"]
    (manager.app_dir(NAME) / "app.json").write_text(json.dumps(manifest), encoding="utf-8")
    manager._write_installed(NAME, record)


def _ledger(state: str, reason: str = "") -> None:
    write_ledger(AdoptionLedger(name=NAME, commit=COMMIT, state=state, reason=reason))


async def _detail_row() -> dict[str, Any]:
    request = make_mocked_request("GET", f"/api/apps/{NAME}", match_info={"name": NAME})
    response = await routes_listing.handle_get_app(request)
    assert response.status == 200
    return json.loads(response.text)


async def _list_rows() -> list[dict[str, Any]]:
    response = await routes_listing.handle_list_apps(make_mocked_request("GET", "/api/apps"))
    assert response.status == 200
    return [row for row in json.loads(response.text) if row["name"] == NAME]


async def _produce() -> dict[str, Any]:
    """Every scenario's rows, in the order the website test reads them."""
    detail: dict[str, Any] = {}
    write_builtin_record()
    for scenario, ledger in (
        ("blocked_admission", ("blocked", "admission_denied")),
        ("blocked_verification", ("blocked", "tree_digest_mismatch")),
        ("pending_network", ("pending", "network_timeout")),
        ("staged", ("staged", "")),
    ):
        _ledger(*ledger)
        detail[scenario] = await _detail_row()
    _adopted_record()
    detail["adopted"] = await _detail_row()
    adopted_list = await _list_rows()
    _adopted_record(version="9.0.0", minKiroCrewVersion="999.0.0")
    detail["adopted_host_too_old"] = await _detail_row()
    return {"detail": detail, "list": {"adopted": adopted_list}}


@pytest.mark.asyncio
async def test_the_website_fixture_holds_the_rows_the_handlers_produce(wheel):
    produced = await _produce()
    # The scenarios the consumer half names must carry the state they claim.
    states = {name: row.get("adoption") for name, row in produced["detail"].items()}
    assert states["blocked_admission"] == {"state": "blocked", "reason": "admission_denied"}
    assert states["adopted"] == {"state": "adopted", "legacyNav": _LEGACY_NAV}
    assert states["adopted_host_too_old"]["hostTooOld"] is True
    assert produced["list"]["adopted"][0]["adoption"] == states["adopted"]
    expected = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    assert produced == expected, (
        f"{_FIXTURE.name} no longer matches what /api/apps answers; replace it with:\n"
        + json.dumps(produced, indent=2, sort_keys=True)
    )


def test_the_dashboard_names_every_reason_code_the_gateway_can_send():
    """``AdoptionReasonCode`` (``components/appstore/types.ts``) is the closed set the
    notice maps to copy; a code only one side knows renders as no notice at all."""
    source = (_WEBSITE_SRC / "components" / "appstore" / "types.ts").read_text(encoding="utf-8")
    union = re.search(r"export type AdoptionReasonCode =(.*?)\r?\n\r?\n", source, re.S)
    assert union is not None, "AdoptionReasonCode is no longer declared in types.ts"
    assert set(re.findall(r"'([a-z_]+)'", union.group(1))) == set(ALL_REASON_CODES)
