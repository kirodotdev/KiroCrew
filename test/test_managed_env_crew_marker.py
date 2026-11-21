"""The list item's ``crew_marker`` is the Crew launch's own ``kc-<6 hex>`` displayName,
a listing hint only: ``own`` stays this home's ledger, and it costs no call."""

from __future__ import annotations

import pytest
from managed_env_fake_bff import FakeBff
from managed_env_helpers import fresh_shared_client, lane_values, no_real_install_ssh  # noqa: F401
from test_managed_env_routes import _env, call, own


@pytest.fixture
def bff():
    b = FakeBff()
    yield b
    b.close()


@pytest.mark.parametrize(
    "name,marker",
    [
        ("kc-0a1b2c", True),
        ("kc-0A1B2C", False),
        ("kc-0a1b2", False),
        ("kc-0a1b2c-1", False),
        ("kc-tag-3", False),
        ("made-in-the-web-app", False),
        ("", False),
    ],
)
def test_crew_marker_grants_nothing_and_costs_no_call(bff, tmp_path, monkeypatch, name, marker):
    routes, reg, guard, _ = _env(bff, tmp_path, monkeypatch)
    mine = own(bff, guard, reg)  # this home's, kc-aaaaaa
    theirs = bff.add_other("RUNNING", name)
    status, body = call(routes, "GET", "/api/managed-environments/list")
    by = {s["sandbox_id"]: s for s in body["sandboxes"]}
    assert status == 200 and bff.ops() == ["ListSandboxes"]
    assert by[mine]["crew_marker"] is True and by[mine]["own"] is True
    assert by[theirs]["crew_marker"] is marker and by[theirs]["own"] is False
    n = len(bff.requests)
    assert call(routes, "POST", f"/api/managed-environments/{theirs}/stop") == (
        404,
        {"error": "not a sandbox of this crew client"},
    )
    assert len(bff.requests) == n
