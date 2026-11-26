"""Tests for the declarative `contributes.sessionCreateItems` contribution point.

Covers parse, round-trip, signature coverage, the per-app cap, malformed input and
the endpoint allowlist: a row whose endpoint leaves `/api/apps/<app>/` is refused at
install, because the dashboard would otherwise POST to it on a row the reader picked.
"""

from __future__ import annotations

import pytest

from kiro_crew.apps.manifest import AppManifest
from kiro_crew.apps.manifest_session_create import MAX_SESSION_CREATE_ITEMS_PER_APP
from kiro_crew.apps.startup_modules import eligibility_refusal

_ITEM = {
    "id": "new-workspace",
    "label": "New workspace session",
    "icon": "Package",
    "endpoint": "/api/apps/workspace-manager/create",
}


def _manifest(items: object) -> AppManifest:
    return AppManifest.from_dict(
        {
            "name": "workspace-manager",
            "version": "1.0.0",
            "displayName": "Workspace Manager",
            "description": "x",
            "contributes": {"sessionCreateItems": items},
        }
    )


def test_round_trip_and_valid():
    m = _manifest([_ITEM])
    assert m.validate() == []
    (item,) = m.contributes.sessionCreateItems.items
    assert (item.id, item.label, item.icon, item.endpoint) == (
        "new-workspace",
        "New workspace session",
        "Package",
        "/api/apps/workspace-manager/create",
    )
    assert m.to_dict()["contributes"]["sessionCreateItems"] == [_ITEM]


def test_absent_block_adds_nothing_to_the_signed_bytes():
    plain = AppManifest.from_dict(
        {"name": "workspace-manager", "version": "1.0.0", "displayName": "W", "description": "x"}
    )
    assert plain.validate() == []
    assert b"sessionCreateItems" not in plain.signing_payload()


def test_endpoint_is_signature_covered():
    payload = _manifest([_ITEM]).signing_payload()
    assert b"sessionCreateItems" in payload
    moved = dict(_ITEM, endpoint="/api/apps/workspace-manager/other")
    assert _manifest([moved]).signing_payload() != payload


@pytest.mark.parametrize(
    "endpoint",
    [
        "/api/shutdown",
        "/api/apps/other-app/create",
        "/api/apps/workspace-manager/../other-app/create",
        "/api/apps/workspace-manager/uninstall",
        "/api/apps/workspace-managerx/create",
        "https://example.com/api/apps/workspace-manager/create",
        "//example.com/api/apps/workspace-manager/create",
        "/apps/workspace-manager/api/create",
        "/api/apps/workspace-manager/create\n",
        "/api/apps/workspace-manager/.\\uninstall",
    ],
)
def test_endpoint_outside_the_apps_own_namespace_is_refused(endpoint: str):
    errors = _manifest([dict(_ITEM, endpoint=endpoint)]).validate()
    assert any("must route under /api/apps/workspace-manager/" in e for e in errors), errors


@pytest.mark.parametrize(
    ("items", "needle"),
    [
        ({"id": "x"}, "must be an array"),
        (["not-an-object"], "must be an object"),
        ([dict(_ITEM, id="Bad_Id")], "kebab slug"),
        ([_ITEM, _ITEM], "declared twice"),
        ([dict(_ITEM, label="")], "label is required"),
        ([dict(_ITEM, label="x" * 121)], "label exceeds"),
        ([dict(_ITEM, icon=3)], "icon must be a string"),
        ([{k: v for k, v in _ITEM.items() if k != "endpoint"}], "endpoint is required"),
        ([dict(_ITEM, extra=1)], "unknown keys"),
        ([dict(_ITEM, id="a" * 65)], "kebab slug"),
        ([dict(_ITEM, endpoint="/api/apps/workspace-manager/" + "a" * 500)], "endpoint exceeds"),
    ],
)
def test_malformed_rows_refuse_the_install(items: object, needle: str):
    errors = _manifest(items).validate()
    assert any(needle in e for e in errors), errors


def test_over_the_cap_is_refused():
    rows = [dict(_ITEM, id=f"row-{i}") for i in range(MAX_SESSION_CREATE_ITEMS_PER_APP + 1)]
    m = _manifest(rows)
    assert any("exceeds the limit" in e for e in m.validate())
    # Nothing of an over-cap list is kept, so the served manifest carries none of it.
    assert m.contributes.sessionCreateItems.items == []
    assert _manifest(rows[:MAX_SESSION_CREATE_ITEMS_PER_APP]).validate() == []


def test_a_browser_only_startup_app_may_not_declare_rows():
    raw = {"name": "workspace-manager", "contributes": {"sessionCreateItems": []}}
    assert "sessionCreateItems" in (eligibility_refusal(raw, "gateway") or "")
