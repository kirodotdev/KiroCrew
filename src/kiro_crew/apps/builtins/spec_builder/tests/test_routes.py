"""Tests for the Spec Builder builtin backend (``backend/routes.py``): the route contract.

Locks in the builtin route contract and the pieces of behaviour that survive
the port from the external app:

  * ``register_routes(app)`` wires the expected ``/api/apps/spec-builder/*``
    method+path set onto the gateway aiohttp Application (builtin contract —
    full paths, no returned AppRoute list), and its startup recovery runs once;
  * the settings endpoint round-trips (GET default → PUT → GET) and rejects a
    relative base_path;
  * spec create validates ``name`` / ``spec_type`` / ``working_dir`` before it
    ever touches gateway state;
  * ``.spec-state.json`` is redaction-scrubbed before it leaves the backend
    (credentials never reach the browser), recursively across nested values;
  * the slot-key prefix is ``spec-builder-`` (renamed from the external app).

The rest of the route suite is split by behaviour into sibling
``test_routes_<topic>.py`` files that share the harness in ``routes_helpers.py``;
a new route test goes in the part whose behaviour it pins.
"""

from __future__ import annotations

import json
import pkgutil

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.apps.builtins.spec_builder import backend as backend_package
from kiro_crew.apps.builtins.spec_builder.tests.routes_facade import BACKEND_MODULES, routes

# _never_touch_the_real_state is an autouse fixture: pytest finds it by the name bound here.
# test_orchestration_composition_contract.py imports _BASE, _decision_spec, _make_client,
# _redirect_state and _slot_stub from here.
from kiro_crew.apps.builtins.spec_builder.tests.routes_helpers import (  # noqa: F401
    _BASE,
    _auth_mw,
    _decision_spec,
    _make_client,
    _never_touch_the_real_state,
    _redirect_state,
    _slot_stub,
)


def test_route_facade_covers_every_backend_module() -> None:
    """Keep aggregate source guards complete when the backend gains a module."""
    discovered = {
        info.name
        for info in pkgutil.walk_packages(
            backend_package.__path__,
            prefix=f"{backend_package.__name__}.",
        )
    }
    listed = {module.__name__ for module in BACKEND_MODULES}
    assert (
        listed == discovered
    ), f"missing={sorted(discovered - listed)}, extra={sorted(listed - discovered)}"


# ── route set (builtin contract) ─────────────────────────────────────────────


def test_register_routes_wires_expected_set(tmp_path, monkeypatch):
    _redirect_state(monkeypatch, tmp_path)
    app = web.Application()
    # Builtin contract: register_routes(app) returns None and registers full
    # paths directly on the router (the external app's AppRoute-list contract
    # was converted during the port). mypy flags asserting a None return
    # (func-returns-value), so just call it.
    startup_before = list(app.on_startup)
    routes.register_routes(app)

    assert (
        list(app.on_startup) == startup_before
    ), "Spec Builder filesystem recovery would block socket readiness"

    wired = {
        (r.method, r.resource.canonical) for r in app.router.routes() if r.resource is not None
    }
    expected = {
        ("GET", f"{_BASE}/settings"),
        ("PUT", f"{_BASE}/settings"),
        ("POST", f"{_BASE}/settings"),
        ("GET", f"{_BASE}/repo-info"),
        ("GET", f"{_BASE}/browse"),
        ("GET", f"{_BASE}/specs"),
        ("POST", f"{_BASE}/specs"),
        ("GET", f"{_BASE}/specs/{{name}}"),
        ("GET", f"{_BASE}/specs/{{name}}/messages"),
        ("POST", f"{_BASE}/specs/{{name}}/message"),
        ("POST", f"{_BASE}/specs/{{name}}/handoff"),
        ("POST", f"{_BASE}/specs/{{name}}/execute"),
        ("POST", f"{_BASE}/specs/{{name}}/stop"),
        # Direct authority over the artifacts: record a phase approval, run one
        # task, and the label / archive / duplicate lifecycle.
        ("POST", f"{_BASE}/specs/{{name}}/approve"),
        ("POST", f"{_BASE}/specs/{{name}}/task"),
        ("POST", f"{_BASE}/specs/{{name}}/title"),
        ("POST", f"{_BASE}/specs/{{name}}/archive"),
        ("POST", f"{_BASE}/specs/{{name}}/duplicate"),
        ("DELETE", f"{_BASE}/specs/{{name}}"),
    }
    assert expected <= wired


@pytest.mark.asyncio
async def test_duplicate_recovery_runs_once_on_first_enabled_request(tmp_path, monkeypatch):
    recovered: list[str] = []
    monkeypatch.setattr(routes, "is_app_enabled", lambda _name: True)
    monkeypatch.setattr(
        routes, "_recover_abandoned_reservations", lambda: recovered.append("recovered")
    )

    async with _make_client(monkeypatch, tmp_path) as client:
        assert (await client.get(f"{_BASE}/settings")).status == 200
        assert (await client.get(f"{_BASE}/specs")).status == 200

    assert recovered == ["recovered"]


def test_slot_key_prefix_renamed():
    # Ported from the external app's 'kiro-specs-' prefix to 'spec-builder-'.
    assert routes._slot_key("demo") == "spec-builder-demo"


# ── settings round-trip ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_settings_roundtrip(tmp_path, monkeypatch):
    async with _make_client(monkeypatch, tmp_path) as client:
        resp = await client.get(f"{_BASE}/settings")
        assert resp.status == 200
        body = await resp.json()
        assert body["base_path"] == ""
        assert body["model"] == ""

        abs_base = str(tmp_path / "specs-home")
        resp = await client.put(f"{_BASE}/settings", json={"base_path": abs_base})
        assert resp.status == 200
        assert (await resp.json()) == {"ok": True, "base_path": abs_base, "model": ""}

        resp = await client.get(f"{_BASE}/settings")
        assert (await resp.json())["base_path"] == abs_base


@pytest.mark.asyncio
async def test_settings_model_roundtrips_and_empty_means_inherit(tmp_path, monkeypatch):
    """The app-wide default model round-trips, and '' round-trips AS '' — an
    empty selection must come back as inherit, not be dropped or persisted as a
    literal model name. An unknown name is kept (availability is only decidable
    in a live session, where the withhold path owns it)."""
    async with _make_client(monkeypatch, tmp_path) as client:
        resp = await client.put(
            f"{_BASE}/settings", json={"base_path": "", "model": "  test-model-x  "}
        )
        assert resp.status == 200
        assert (await resp.json()) == {"ok": True, "base_path": "", "model": "test-model-x"}

        resp = await client.get(f"{_BASE}/settings")
        assert (await resp.json())["model"] == "test-model-x"

        # Clearing the pick round-trips back to inherit.
        resp = await client.put(f"{_BASE}/settings", json={"base_path": "", "model": ""})
        assert resp.status == 200
        resp = await client.get(f"{_BASE}/settings")
        assert (await resp.json())["model"] == ""


@pytest.mark.asyncio
async def test_settings_write_without_model_key_preserves_the_stored_model(tmp_path, monkeypatch):
    """settings.json predates the model field, so a legacy client PUTting only
    base_path must not silently erase a configured model. Absence preserves;
    clearing requires an explicit ''."""
    async with _make_client(monkeypatch, tmp_path) as client:
        resp = await client.put(
            f"{_BASE}/settings", json={"base_path": "", "model": "test-model-x"}
        )
        assert resp.status == 200

        # Legacy-shaped write: no model key at all.
        resp = await client.put(f"{_BASE}/settings", json={"base_path": ""})
        assert resp.status == 200
        assert (await resp.json())["model"] == "test-model-x"

        resp = await client.get(f"{_BASE}/settings")
        assert (await resp.json())["model"] == "test-model-x", "an omitted key erased the model"


@pytest.mark.asyncio
async def test_settings_rejects_malformed_model(tmp_path, monkeypatch):
    """Mirrors the Research app's write contract: a non-string is a 400 that
    names the problem, and an over-length id is rejected rather than truncated
    (a sliced id is a different string that is never served)."""
    async with _make_client(monkeypatch, tmp_path) as client:
        resp = await client.put(
            f"{_BASE}/settings", json={"base_path": "", "model": ["not", "a", "string"]}
        )
        assert resp.status == 400
        assert (await resp.json())["code"] == "model_not_a_string"

        resp = await client.put(
            f"{_BASE}/settings",
            json={"base_path": "", "model": "m" * (routes._MAX_MODEL_LEN + 1)},
        )
        assert resp.status == 400
        assert (await resp.json())["code"] == "model_too_long"

        # GET serves the field through _redact; its fail-closed placeholder must
        # not be storable as a model if a client round-trips the read back.
        resp = await client.put(
            f"{_BASE}/settings", json={"base_path": "", "model": routes._UNSCRUBBABLE}
        )
        assert resp.status == 400
        assert (await resp.json())["code"] == "model_invalid"


def test_load_settings_degrades_malformed_model_to_inherit(tmp_path, monkeypatch):
    """settings.json is agent-writable, so its model FIELD is untrusted like its
    shape: a list, a number, or an over-length string loads as '' (= inherit),
    mirroring the base_path hardening at the same chokepoint."""
    settings_path = tmp_path / "settings.json"
    monkeypatch.setattr(routes, "_SETTINGS_PATH", settings_path)
    bad_values: list[object] = [[], 7, None, "m" * (routes._MAX_MODEL_LEN + 1)]
    for bad in bad_values:
        settings_path.write_text(json.dumps({"base_path": "", "model": bad}))
        assert routes._load_settings()["model"] == "", f"{bad!r} did not degrade"
    # A sane value survives the same chokepoint, trimmed.
    settings_path.write_text(json.dumps({"base_path": "", "model": " test-model-x "}))
    assert routes._load_settings()["model"] == "test-model-x"


@pytest.mark.asyncio
async def test_settings_rejects_relative_base_path(tmp_path, monkeypatch):
    async with _make_client(monkeypatch, tmp_path) as client:
        resp = await client.put(f"{_BASE}/settings", json={"base_path": "not/absolute"})
        assert resp.status == 400


@pytest.mark.asyncio
async def test_settings_requires_auth(tmp_path, monkeypatch):
    # No auth middleware -> _require_auth returns 401.
    _redirect_state(monkeypatch, tmp_path)
    app = web.Application()
    routes.register_routes(app)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(f"{_BASE}/settings")
        assert resp.status == 401


@pytest.mark.asyncio
async def test_disabled_app_denies(tmp_path, monkeypatch):
    _redirect_state(monkeypatch, tmp_path)
    monkeypatch.setattr(routes, "is_app_enabled", lambda name: False)
    app = web.Application(middlewares=[_auth_mw])
    routes.register_routes(app)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(f"{_BASE}/settings")
        assert resp.status == 403


# ── create validation ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_rejects_relative_working_dir(tmp_path, monkeypatch):
    async with _make_client(monkeypatch, tmp_path) as client:
        resp = await client.post(
            f"{_BASE}/specs",
            json={"name": "demo", "working_dir": "relative/path"},
        )
        assert resp.status == 400
        assert "working_dir" in (await resp.json())["error"]


@pytest.mark.asyncio
async def test_create_rejects_bad_name(tmp_path, monkeypatch):
    async with _make_client(monkeypatch, tmp_path) as client:
        resp = await client.post(
            f"{_BASE}/specs",
            json={"name": "bad name!", "working_dir": str(tmp_path)},
        )
        assert resp.status == 400


@pytest.mark.asyncio
async def test_create_rejects_missing_working_dir(tmp_path, monkeypatch):
    async with _make_client(monkeypatch, tmp_path) as client:
        # Absolute but non-existent path.
        resp = await client.post(
            f"{_BASE}/specs",
            json={"name": "demo", "working_dir": str(tmp_path / "nope")},
        )
        assert resp.status == 400


def test_valid_name_rules():
    assert routes._valid_name("feature-1")
    assert routes._valid_name("a_b-C9")
    assert not routes._valid_name("bad name")
    assert not routes._valid_name("-leading")
    assert not routes._valid_name("")


# ── redaction ────────────────────────────────────────────────────────────────


def test_redact_scrubs_credentials():
    secret = "glpat-IOSFODNN7EXAMPLE"
    out = routes._redact(f"key is {secret} ok")
    assert secret not in out


@pytest.mark.asyncio
async def test_spec_state_is_redacted_on_get(tmp_path, monkeypatch):
    _redirect_state(monkeypatch, tmp_path)
    monkeypatch.setattr(routes, "is_app_enabled", lambda name: True)

    # A spec on disk whose .spec-state.json carries a credential in a nested
    # value — the GET handler must recursively scrub it before serving.
    spec_dir = tmp_path / "wd" / ".kiro" / "specs" / "demo"
    spec_dir.mkdir(parents=True)
    secret = "glpat-IOSFODNN7EXAMPLE"
    (spec_dir / ".spec-state.json").write_text(
        json.dumps(
            {
                "decisions": [{"id": "x", "title": f"use {secret} here", "answer": None}],
                "blocking": None,
                "context": {"template": "webex"},
            }
        )
    )
    routes._save_index(
        {
            "demo": {
                "working_dir": str(tmp_path / "wd"),
                "spec_dir": str(spec_dir),
                "spec_type": "feature",
                "status": "planning",
                "slot_key": routes._slot_key("demo"),
            }
        }
    )

    app = web.Application(middlewares=[_auth_mw])
    routes.register_routes(app)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(f"{_BASE}/specs/demo")
        assert resp.status == 200
        data = await resp.json()
        # Credential scrubbed from the nested decision title.
        assert secret not in json.dumps(data["state"])
        assert data["state"]["context"]["template"] == "webex"


@pytest.mark.asyncio
async def test_get_unknown_spec_404(tmp_path, monkeypatch):
    async with _make_client(monkeypatch, tmp_path) as client:
        resp = await client.get(f"{_BASE}/specs/ghost")
        assert resp.status == 404
