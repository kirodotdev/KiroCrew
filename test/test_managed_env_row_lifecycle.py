"""The row lifecycle (on with the lane): its reader, the list answer's field,
the SPA's provisioner stamp, and another client's sandbox with the flag on (a regression row).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from managed_env_fake_bff import FakeBff
from managed_env_helpers import fresh_shared_client, lane_values, no_real_install_ssh  # noqa: F401
from test_managed_env_routes import _env, call, own

from kiro_crew.managed_env import PROVISIONER_ID, core_signin

_ROW_ACTIONS_TSX = (
    Path(__file__).resolve().parents[1]
    / "website/src/components/managedEnv/SandboxCrewRowActions.tsx"
)
_MISSING = object()


@pytest.fixture
def bff():
    b = FakeBff()
    yield b
    b.close()


def _write(monkeypatch, tmp_path, value, *, raw=None):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    cfg = tmp_path / "config.json"
    if raw is not None:
        cfg.write_text(raw)
    elif value is _MISSING:
        cfg.unlink(missing_ok=True)
    else:
        cfg.write_text(json.dumps({"sandbox": {"lane": {"enabled": True, "row_lifecycle": value}}}))


def test_the_list_answer_says_row_lifecycle_on_by_default(bff, tmp_path, monkeypatch):
    routes, reg, guard, _ = _env(bff, tmp_path, monkeypatch)
    _write(monkeypatch, tmp_path, _MISSING)  # the real reader, no key at all
    own(bff, guard, reg)
    status, body = call(routes, "GET", "/api/managed-environments/list")
    assert status == 200 and body["row_lifecycle"] is True


def test_with_the_lane_off_the_default_on_flag_is_absent(bff, tmp_path, monkeypatch):
    """The flag defaults on only behind the lane switch, which stays off by default."""
    from kiro_crew.managed_env.routes import SandboxRoutes

    routes, reg, guard, _ = _env(bff, tmp_path, monkeypatch)
    _write(monkeypatch, tmp_path, _MISSING)  # no lane section, no row_lifecycle key
    monkeypatch.setattr("kiro_crew.managed_env.settings.lane_enabled", lambda: False)
    assert routes.list_sync()["row_lifecycle"] is False
    flag_routes = SandboxRoutes(_no_engine, run_in_thread=False)
    assert _flag_call(flag_routes, monkeypatch, lane=False) == (404, {"error": "not found"})


def test_the_list_answer_says_row_lifecycle_on_from_the_config(bff, tmp_path, monkeypatch):
    routes, reg, guard, _ = _env(bff, tmp_path, monkeypatch)
    _write(monkeypatch, tmp_path, True)  # the real reader, not a stand-in
    own(bff, guard, reg)
    status, body = call(routes, "GET", "/api/managed-environments/list")
    assert status == 200 and body["row_lifecycle"] is True and body["error"] == ""


def test_row_lifecycle_applies_only_while_the_lane_is_on(bff, tmp_path, monkeypatch):
    routes, reg, guard, _ = _env(bff, tmp_path, monkeypatch)
    monkeypatch.setattr("kiro_crew.managed_env.settings.lane_enabled", lambda: True)
    assert routes.list_sync()["row_lifecycle"] is True
    monkeypatch.setattr("kiro_crew.managed_env.settings.lane_enabled", lambda: False)
    assert routes.list_sync()["row_lifecycle"] is False


def test_another_clients_sandbox_with_the_flag_on_is_listed_and_refused(bff, tmp_path, monkeypatch):
    """The form's list is cut by crew row; another client's sandbox has none and stays."""
    routes, reg, guard, _ = _env(bff, tmp_path, monkeypatch)
    monkeypatch.setattr("kiro_crew.managed_env.settings.lane_enabled", lambda: True)
    mine = own(bff, guard, reg)
    other = bff.add_other("RUNNING")
    status, body = call(routes, "GET", "/api/managed-environments/list")
    assert status == 200 and body["row_lifecycle"] is True
    by = {s["sandbox_id"]: s for s in body["sandboxes"]}
    assert by[other]["own"] is False and by[other]["instance_id"] == ""
    assert by[mine]["own"] is True and by[mine]["instance_id"] == f"row-{mine}"
    n = len(bff.requests)
    for action in ("stop", "resume", "delete"):
        assert call(routes, "POST", f"/api/managed-environments/{other}/{action}") == (
            404,
            {"error": "not a sandbox of this crew client"},
        )
    assert len(bff.requests) == n and bff.naming(other) == []
    assert routes.actions.get(other) == {}


def _flag_call(routes, monkeypatch, *, lane=True, owner=True):
    from aiohttp import web as _web

    from kiro_crew.dashboard.handlers import _shared

    async def gate(request, operation):
        return None if owner else _web.json_response({"error": "owner_only"}, status=403)

    monkeypatch.setattr(_shared, "require_owner_dashboard_request", gate)
    monkeypatch.setattr("kiro_crew.managed_env.settings.lane_enabled", lambda: lane)
    return call(routes, "GET", "/api/managed-environments/row-lifecycle")


# An empty vault (KIROCREW_HOME is tmp_path): the token-free sign-in status.
_SIGNED_OUT = {
    "sign_in": {
        "state": "signin_required",
        "message": core_signin.NOT_SIGNED_IN,
        "local_agent": "on",
    }
}


def _no_engine():
    raise AssertionError("the flag route built the engine (it reads the config only)")


def test_the_flag_route_is_absent_with_the_lane_off_and_owner_only(bff, monkeypatch, tmp_path):
    from kiro_crew.managed_env.routes import SandboxRoutes

    routes = SandboxRoutes(_no_engine, run_in_thread=False)
    _write(monkeypatch, tmp_path, True)
    assert _flag_call(routes, monkeypatch, lane=False) == (404, {"error": "not found"})
    assert _flag_call(routes, monkeypatch, owner=False) == (403, {"error": "owner_only"})


def test_the_flag_route_makes_no_bff_call(bff, tmp_path, monkeypatch):
    routes, reg, guard, _ = _env(bff, tmp_path, monkeypatch)
    own(bff, guard, reg)
    bff.add_other("RUNNING")
    monkeypatch.setattr("kiro_crew.managed_env.settings.lane_enabled", lambda: True)
    got = call(routes, "GET", "/api/managed-environments/row-lifecycle")
    assert got == (200, {"row_lifecycle": True, **_SIGNED_OUT})
    assert bff.requests == []


def test_the_spa_keys_its_row_actions_by_the_lanes_provisioner_id():
    """registerCrewRowActions matches the row's stamp verbatim: a drift draws nothing, silently."""
    text = _ROW_ACTIONS_TSX.read_text(encoding="utf-8")
    found = re.findall(r"export const SANDBOX_PROVISIONER_ID = '([^']*)'", text)
    assert found == [PROVISIONER_ID]
