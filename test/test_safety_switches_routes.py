"""Settings -> Security -> Safety switches: the owner-only routes for the two MCP
safety keys, ``mcp.protect_kiro_permission_rules`` and ``mcp.honour_auto_approve``.

What is pinned: only the dashboard owner reads or changes them (on both verbs);
only those two keys are accepted; each change is audited synchronously before it
is written, and a change whose audit cannot be written does not happen; a change
lands in ``config.json`` and in the live read each enforcing gate makes.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers import safety_switches as handlers

pytestmark = pytest.mark.asyncio

_PROTECT = "mcp.protect_kiro_permission_rules"
_HONOUR = "mcp.honour_auto_approve"
_URL = "/api/security/safety-switches"


def _make_app(*, owner: bool) -> web.Application:
    @web.middleware
    async def _identity(request, handler):
        # The owner: a machine-local bootstrap subject with no owner_id set.
        # A non-owner: an allow-listed dashboard session for someone else.
        request["user"] = "local-app" if owner else "someone-else"
        request["app"] = ""
        request.app["state"].owner_id = "" if owner else "owner-1"
        return await handler(request)

    app = web.Application(middlewares=[_identity])
    app["state"] = MagicMock()
    app.router.add_get(_URL, handlers.api_safety_switches_get)
    app.router.add_put(_URL, handlers.api_safety_switches_put)
    return app


@pytest.fixture
def tmp_config(tmp_path, monkeypatch):
    from kiro_crew.config import live

    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"mcp": {}}), encoding="utf-8")
    monkeypatch.setattr(live, "snapshot", lambda: None)
    with (
        patch("kiro_crew.config.loader.config_path", return_value=cfg_path),
        patch("kiro_crew.config.loader.overlay_pins", return_value=False),
    ):
        yield cfg_path


@pytest.fixture
def audits(monkeypatch):
    events: list[dict] = []

    def _record(**kw):
        events.append(kw)
        return True

    monkeypatch.setattr(handlers, "_audit", _record, raising=True)
    return events


async def _call(method: str, *, owner: bool = True, body=None) -> tuple[int, dict]:
    async with TestClient(TestServer(_make_app(owner=owner))) as client:
        resp = await client.request(method, _URL, json=body)
        return resp.status, await resp.json()


def _by_key(state: dict) -> dict[str, dict]:
    return {s["key"]: s for s in state["switches"]}


def _stored(cfg_path) -> dict:
    return json.loads(cfg_path.read_text(encoding="utf-8")).get("mcp", {})


async def test_owner_get_returns_both_switches_at_their_defaults(tmp_config, audits) -> None:
    status, state = await _call("GET")
    assert status == 200, state
    switches = _by_key(state)
    assert set(switches) == {_PROTECT, _HONOUR}
    assert switches[_PROTECT]["enabled"] is False and switches[_PROTECT]["in_force"] is False
    assert switches[_HONOUR]["enabled"] is True and switches[_HONOUR]["in_force"] is True
    assert state["degraded"] is False
    assert any(a["operation"] == "read" and a["outcome"] == "allowed" for a in audits)


@pytest.mark.parametrize("key", [_PROTECT, _HONOUR])
async def test_owner_put_flips_the_key_in_config_and_in_the_live_gate(
    tmp_config, audits, key
) -> None:
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.platform.governance import _auto_approve_is_honoured
    from kiro_crew.security.paths import _permission_rule_rows

    def gate() -> bool:
        return bool(_permission_rule_rows()) if key == _PROTECT else _auto_approve_is_honoured()

    leaf = key.split(".")[1]
    before = gate()
    status, state = await _call("PUT", body={"key": key, "enabled": not before})
    assert status == 200, state
    assert _stored(tmp_config)[leaf] is (not before)
    assert getattr(KiroCrewConfig.load().mcp, leaf) is (not before)
    assert gate() is (not before)
    assert _by_key(state)[key]["enabled"] is (not before)
    # The change record is synchronous (critical) and names the key.
    change = [a for a in audits if a["outcome"] in ("enabled", "disabled")]
    assert change and change[0]["critical"] is True and change[0]["detail"] == key

    status, _ = await _call("PUT", body={"key": key, "enabled": before})
    assert status == 200
    assert gate() is before


@pytest.mark.parametrize(
    "body",
    [
        {"key": "agent.dangerously_skip_permissions", "enabled": True},
        {"key": "mcp.extra_path_dirs", "enabled": True},
        {"key": "honour_auto_approve", "enabled": True},
        {"enabled": True},
        {"key": _PROTECT, "enabled": "true"},
        {"key": _PROTECT},
        [_PROTECT, True],
    ],
)
async def test_put_refuses_anything_but_one_switch_and_a_boolean(tmp_config, audits, body) -> None:
    status, _ = await _call("PUT", body=body)
    assert status == 400
    assert _stored(tmp_config) == {}
    assert "dangerously_skip_permissions" not in tmp_config.read_text(encoding="utf-8")


@pytest.mark.parametrize("method", ["GET", "PUT"])
async def test_a_non_owner_is_refused_on_both_verbs(tmp_config, audits, method) -> None:
    body = {"key": _PROTECT, "enabled": True} if method == "PUT" else None
    status, payload = await _call(method, owner=False, body=body)
    assert status == 403
    assert payload["code"] == "dashboard_owner_required"
    assert _stored(tmp_config) == {}
    assert [a["outcome"] for a in audits] == ["denied"]


@pytest.mark.parametrize("enabled", [True, False])
async def test_a_change_whose_audit_cannot_be_written_does_not_happen(
    tmp_config, monkeypatch, enabled
) -> None:
    seen: list[dict] = []

    def failing(**kw):
        seen.append(kw)
        return False

    monkeypatch.setattr(handlers, "_audit", failing, raising=True)
    status, payload = await _call("PUT", body={"key": _HONOUR, "enabled": enabled})
    assert status == 503
    assert payload["code"] == "safety_switch_audit_unavailable"
    assert seen and seen[0]["critical"] is True
    assert _stored(tmp_config) == {}


async def test_a_key_config_local_json_sets_is_refused(tmp_config, audits) -> None:
    with patch("kiro_crew.config.loader.overlay_pins", return_value=True):
        status, payload = await _call("PUT", body={"key": _PROTECT, "enabled": True})
    assert status == 409
    assert payload["code"] == "config_overlay_owned"
    assert _stored(tmp_config) == {}


async def test_get_says_when_the_config_read_is_degraded(tmp_config, audits, monkeypatch) -> None:
    """A degraded read keeps the fence on; the route reports that as it acts."""
    from kiro_crew.config import live
    from kiro_crew.config.loader import KiroCrewConfig

    cfg = KiroCrewConfig()
    cfg._degraded_sections = frozenset({"mcp"})
    monkeypatch.setattr(live, "snapshot", lambda: cfg)
    status, state = await _call("GET")
    assert status == 200
    assert state["degraded"] is True
    protect = _by_key(state)[_PROTECT]
    assert protect["enabled"] is False and protect["in_force"] is True


async def test_the_routes_are_registered_on_the_dashboard() -> None:
    from kiro_crew.dashboard.routes import system

    app_stub = MagicMock()
    system.register(app_stub)
    router = app_stub.router
    registered = {
        (verb, c.args[1].__name__)
        for verb, calls in (("GET", router.add_get), ("PUT", router.add_put))
        for c in calls.call_args_list
        if c.args and c.args[0] == _URL
    }
    assert registered == {("GET", "api_safety_switches_get"), ("PUT", "api_safety_switches_put")}


async def test_a_pending_outside_edit_refuses_the_save_before_any_change_record(
    tmp_config, audits, monkeypatch
) -> None:
    """A config.json edit waiting for review refuses the save with its own code,
    and the SEL gets no enabled/disabled record for a change that never happened."""
    from kiro_crew.config import loosening_provenance

    monkeypatch.setattr(loosening_provenance, "leaf_pending", lambda leaf: True)
    monkeypatch.setattr(loosening_provenance, "PROTECTED_LEAVES", {"config.json"})
    status, payload = await _call("PUT", body={"key": _PROTECT, "enabled": True})
    assert status == 409, payload
    assert payload["code"] == "file_pending_review"
    assert _stored(tmp_config) == {}
    assert [a for a in audits if a["outcome"] in ("enabled", "disabled")] == []
    assert any(a["outcome"] == "denied" for a in audits)


async def test_a_refusal_raised_inside_the_write_is_not_a_server_error(
    tmp_config, audits, monkeypatch
) -> None:
    from kiro_crew.config import loader
    from kiro_crew.config.loader import ConfigWriteRefused

    class _Refused(ConfigWriteRefused):
        code = "write_refused_for_test"

    def refuse(*_a, **_kw):
        raise _Refused("refused for test")

    monkeypatch.setattr(loader, "update_config_locked", refuse)
    status, payload = await _call("PUT", body={"key": _HONOUR, "enabled": False})
    assert status == 409, payload
    assert payload == {"error": "refused for test", "code": "write_refused_for_test"}
