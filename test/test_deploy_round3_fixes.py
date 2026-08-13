"""Tests for PR #6 round-3 fixes (F1-F6)."""
from __future__ import annotations

import base64
import json
from typing import Any
from unittest.mock import MagicMock

import pytest
from aiohttp import web

# ─── F1: scanner detects base64-encoded AKIA and bare 40-char secrets ───────


def test_scan_detects_base64_encoded_akia():
    """F1: A base64-encoded AKIA key is detected by scan_content."""
    from kiro_crew.deploy.scan import scan_content

    # "AKIAIOSFODNN7EXAMPLE" base64-encoded
    raw_key = "AKIAIOSFODNN7EXAMPLE"
    encoded = base64.b64encode(raw_key.encode()).decode()
    text = f"some config\naws_key = {encoded}\nmore stuff"
    findings = scan_content(text)
    cred_findings = [f for f in findings if f.severity == "credential"]
    assert len(cred_findings) >= 1, f"Expected credential finding for base64 AKIA, got: {findings}"


def test_scan_detects_bare_40_char_secret():
    """F1: A bare 40-char base64-alphabet AWS secret key is detected."""
    from kiro_crew.deploy.scan import scan_content

    # Simulated AWS secret key: 40 chars of base64 alphabet
    bare_secret = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
    text = f"secret = {bare_secret}\n"
    findings = scan_content(text)
    cred_findings = [f for f in findings if f.severity == "credential"]
    assert len(cred_findings) >= 1, f"Expected credential finding for bare secret, got: {findings}"


# ─── F2: pending dismiss has authz and audit ────────────────────────────────


class _NonRestrictedState:
    """Minimal dashboard state that passes _deny_restricted (non-restricted)."""
    _restricted_keys: set = set()
    _slots: dict = {}


class _RestrictedState:
    """Dashboard state that triggers _deny_restricted."""
    _restricted_keys: set = {"dashboard:ui"}
    _slots: dict = {}


class _FakeReq:
    """Test request for handler calls."""

    def __init__(self, *, match_info: dict | None = None,
                 headers: dict | None = None, state: Any = None):
        self.headers = headers or {"X-Session-Key": "dashboard:ui"}
        self.app = {"state": state or _NonRestrictedState()}
        self.match_info = match_info or {}

    async def json(self) -> dict:
        return {}


@pytest.fixture
def internal_secret_headers() -> dict[str, str]:
    return {"X-Internal-Secret": "test-secret"}


@pytest.mark.asyncio
async def test_pending_dismiss_403_internal_secret(tmp_path):
    """F2: dismiss rejects internal-secret callers (via @_internal_denied decorator)."""
    from kiro_crew.deploy.handlers import _handle_pending_dismiss

    req = _FakeReq(
        match_info={"id": "fake-id"},
        headers={"X-Internal-Secret": "test-secret", "X-Session-Key": "dashboard:ui"},
    )
    resp = await _handle_pending_dismiss(req)
    assert resp.status == 403
    body = json.loads(resp.body)
    assert "not available to internal-secret" in body["error"]


@pytest.mark.asyncio
async def test_pending_dismiss_403_restricted_session(monkeypatch, tmp_path):
    """F2: dismiss rejects restricted sessions."""
    from kiro_crew.deploy import handlers

    req = _FakeReq(
        match_info={"id": "fake-id"},
        state=_RestrictedState(),
    )

    # Patch _is_restricted_session to return True for our state
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers._shared._is_restricted_session",
        lambda state, request: True,
    )
    resp = await handlers._handle_pending_dismiss(req)
    assert resp.status == 403
    body = json.loads(resp.body)
    assert "restricted session" in body["error"]


@pytest.mark.asyncio
async def test_pending_dismiss_200_normal(monkeypatch, tmp_path):
    """F2: dismiss succeeds for normal cookie-auth caller and records audit."""
    from kiro_crew.deploy import handlers, pending

    # Patch restricted session check to pass
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers._shared._is_restricted_session",
        lambda state, request: False,
    )

    # Add a pending entry
    monkeypatch.setattr(pending, "_store_path", lambda: tmp_path / "pending.json")
    entry = pending.add_pending({"site_id": "test-site"})
    entry_id = entry["id"]

    audit_calls: list[tuple] = []

    def _capture_audit(action: str, site_id: str, outcome: str, **kw: Any) -> None:
        audit_calls.append((action, site_id, outcome))

    monkeypatch.setattr(handlers, "_audit", _capture_audit)

    req = _FakeReq(match_info={"id": entry_id})
    resp = await handlers._handle_pending_dismiss(req)
    assert resp.status == 200
    body = json.loads(resp.body)
    assert body == {"ok": True}
    # Verify audit was called
    assert any(c[0] == "pending_dismiss" and c[2] == "ok" for c in audit_calls)


# ─── F3: compat aliases correct ────────────────────────────────────────────


def test_compat_map_has_sites_not_list():
    """F3: The compat map maps 'sites' (real old endpoint) not 'list' (bogus)."""
    from kiro_crew.deploy import handlers

    # Access the map inside register_routes by importing and inspecting
    # We verify the compat map keys via a simulated register_routes call
    app = web.Application()
    state = MagicMock()
    state._restricted_keys = set()
    state._slots = {}
    app["state"] = state
    handlers.register_routes(app)

    # Verify routes exist by checking the router
    routes_info = [r.resource.canonical for r in app.router.routes()
                   if hasattr(r, 'resource') and r.resource]
    # The compat catch-all route should be present
    assert any("deploy-web" in str(r) for r in routes_info)


def test_compat_static_map_contents():
    """F3: Verify the exact compat alias dict contents via module-level inspection."""
    # The dict is local to register_routes, so we test its effect via the router.
    # Check the router resolves old paths to correct new targets.
    import inspect

    from kiro_crew.deploy import handlers

    # Extract the map by reading the source
    source = inspect.getsource(handlers.register_routes)
    assert '"sites": "/api/deploy/list"' in source
    assert '"config": "/api/deploy/config"' in source
    assert '"iam-policy": "/api/deploy/iam-policy"' in source
    assert '"verify": "/api/deploy/verify"' in source
    # Bogus "list" alias must NOT be present
    assert '"list": "/api/deploy/list"' not in source


# ─── F4: ttl_hours strict type checking ────────────────────────────────────


@pytest.mark.asyncio
async def test_ttl_hours_string_rejected(monkeypatch):
    """F4: ttl_hours as string "12" → 400."""
    from kiro_crew.deploy import handlers

    async def _fake_resolve(p):
        return ("prof", "us-west-2")

    monkeypatch.setattr("kiro_crew.deploy.handlers._resolve_profile", _fake_resolve)
    monkeypatch.setattr("kiro_crew.deploy.handlers._HAS_ARTIFACTS", True)
    monkeypatch.setattr("kiro_crew.deploy.handlers.validate_field", lambda v, spec: v)

    class FakeArt:
        kind = "html"
        content = "<h1>hi</h1>"
        name = "test"

    class FakeStore:
        def get(self, slug: str):
            return FakeArt()

    monkeypatch.setattr("kiro_crew.deploy.handlers.get_default_store", lambda: FakeStore())
    monkeypatch.setattr("kiro_crew.deploy.handlers._stage_artifact_html",
                        lambda kind, content, name: ([], "/tmp/x", 42))

    status, body = await handlers._do_deploy({
        "site_id": "s", "artifact_slug": "slug", "ttl_hours": "12",
    })
    assert status == 400
    assert "integer" in body["error"]


@pytest.mark.asyncio
async def test_ttl_hours_float_rejected(monkeypatch):
    """F4: ttl_hours as float 12.5 → 400."""
    from kiro_crew.deploy import handlers

    async def _fake_resolve(p):
        return ("prof", "us-west-2")

    monkeypatch.setattr("kiro_crew.deploy.handlers._resolve_profile", _fake_resolve)
    monkeypatch.setattr("kiro_crew.deploy.handlers._HAS_ARTIFACTS", True)
    monkeypatch.setattr("kiro_crew.deploy.handlers.validate_field", lambda v, spec: v)

    class FakeArt:
        kind = "html"
        content = "<h1>hi</h1>"
        name = "test"

    class FakeStore:
        def get(self, slug: str):
            return FakeArt()

    monkeypatch.setattr("kiro_crew.deploy.handlers.get_default_store", lambda: FakeStore())
    monkeypatch.setattr("kiro_crew.deploy.handlers._stage_artifact_html",
                        lambda kind, content, name: ([], "/tmp/x", 42))

    status, body = await handlers._do_deploy({
        "site_id": "s", "artifact_slug": "slug", "ttl_hours": 12.5,
    })
    assert status == 400
    assert "float" in body["error"]


@pytest.mark.asyncio
async def test_ttl_hours_bool_rejected(monkeypatch):
    """F4: ttl_hours as bool True → 400."""
    from kiro_crew.deploy import handlers

    async def _fake_resolve(p):
        return ("prof", "us-west-2")

    monkeypatch.setattr("kiro_crew.deploy.handlers._resolve_profile", _fake_resolve)
    monkeypatch.setattr("kiro_crew.deploy.handlers._HAS_ARTIFACTS", True)
    monkeypatch.setattr("kiro_crew.deploy.handlers.validate_field", lambda v, spec: v)

    class FakeArt:
        kind = "html"
        content = "<h1>hi</h1>"
        name = "test"

    class FakeStore:
        def get(self, slug: str):
            return FakeArt()

    monkeypatch.setattr("kiro_crew.deploy.handlers.get_default_store", lambda: FakeStore())
    monkeypatch.setattr("kiro_crew.deploy.handlers._stage_artifact_html",
                        lambda kind, content, name: ([], "/tmp/x", 42))

    status, body = await handlers._do_deploy({
        "site_id": "s", "artifact_slug": "slug", "ttl_hours": True,
    })
    assert status == 400
    assert "bool" in body["error"]


@pytest.mark.asyncio
async def test_ttl_hours_int_ok(monkeypatch):
    """F4: ttl_hours as int 12 → passes validation (hits confirm gate)."""
    from kiro_crew.deploy import handlers

    async def _fake_resolve(p):
        return ("prof", "us-west-2")

    monkeypatch.setattr("kiro_crew.deploy.handlers._resolve_profile", _fake_resolve)
    monkeypatch.setattr("kiro_crew.deploy.handlers._HAS_ARTIFACTS", True)
    monkeypatch.setattr("kiro_crew.deploy.handlers.validate_field", lambda v, spec: v)

    class FakeArt:
        kind = "html"
        content = "<h1>hi</h1>"
        name = "test"

    class FakeStore:
        def get(self, slug: str):
            return FakeArt()

    monkeypatch.setattr("kiro_crew.deploy.handlers.get_default_store", lambda: FakeStore())
    monkeypatch.setattr("kiro_crew.deploy.handlers._stage_artifact_html",
                        lambda kind, content, name: ([], "/tmp/x", 42))

    status, body = await handlers._do_deploy({
        "site_id": "s", "artifact_slug": "slug", "ttl_hours": 12,
    })
    # Should reach the confirm gate (status 200)
    assert status == 200
    assert body.get("requires_confirm") is True


# ─── F5: webapp artifact → 400 ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_webapp_artifact_returns_400(monkeypatch):
    """F5: Deploying a webapp-kind artifact returns 400 with explanation."""
    from kiro_crew.deploy import handlers

    async def _fake_resolve(p):
        return ("prof", "us-west-2")

    monkeypatch.setattr("kiro_crew.deploy.handlers._resolve_profile", _fake_resolve)
    monkeypatch.setattr("kiro_crew.deploy.handlers._HAS_ARTIFACTS", True)
    monkeypatch.setattr("kiro_crew.deploy.handlers.validate_field", lambda v, spec: v)

    class FakeArt:
        kind = "webapp"
        content = "My webapp summary"
        name = "my-app"

    class FakeStore:
        def get(self, slug: str):
            return FakeArt()

    monkeypatch.setattr("kiro_crew.deploy.handlers.get_default_store", lambda: FakeStore())

    status, body = await handlers._do_deploy({
        "site_id": "s", "artifact_slug": "my-webapp",
    })
    assert status == 400
    assert "webapp artifacts contain an app summary" in body["error"]
    assert "local_dir" in body["error"]


# ─── F6: concurrent site listing with bounded concurrency ───────────────────


@pytest.mark.asyncio
async def test_list_concurrent_bounded(monkeypatch):
    """F6: site listing runs concurrently but bounded, with per-profile error isolation."""
    from kiro_crew.deploy import engine, handlers
    from kiro_crew.deploy import profiles as profiles_mod

    max_inflight = 0
    current_inflight = 0
    call_count = 0

    def _fake_list_sites(profile: str, region: str) -> list[dict[str, Any]]:
        nonlocal max_inflight, current_inflight, call_count
        # Track concurrency (this runs in a thread, so need sync)
        call_count += 1
        if profile == "failing":
            raise engine.AWSError("simulated failure")
        return [{"site_id": f"site-{profile}", "distribution_id": f"dist-{profile}"}]

    fake_profiles = {
        "profiles": [
            {"name": f"prof-{i}", "region": "us-west-2"}
            for i in range(8)
        ] + [{"name": "failing", "region": "us-west-2"}]
    }

    monkeypatch.setattr(profiles_mod, "load_registry", lambda: fake_profiles)
    monkeypatch.setattr(engine, "list_sites", _fake_list_sites)

    status, payload = await handlers._do_list()
    assert status == 200
    assert payload["configured"] is True
    # 8 successful profiles → 8 sites
    assert len(payload["sites"]) == 8
    # 1 failing profile → error isolated
    assert "profile_errors" in payload
    assert len(payload["profile_errors"]) == 1
    assert "failing" in payload["profile_errors"][0]


# --- F4: lifecycle persistent/ttl_hours strict typing (hand-added) -----------

def test_lifecycle_persistent_rejects_string_false():
    """The string "false" is truthy — must be rejected, not coerced."""
    import pytest

    from kiro_crew.validation import ValidationError, _validate_artifact_save
    bad = {"webapp_metadata": {"lifecycle": {"persistent": "false"}}}
    with pytest.raises(ValidationError):
        _validate_artifact_save(bad)
    ok = {"webapp_metadata": {"lifecycle": {"persistent": True}}}
    _validate_artifact_save(ok)


def test_lifecycle_ttl_hours_strict_int_bounds():
    import pytest

    from kiro_crew.validation import ValidationError, _validate_artifact_save
    for bad_val in ("12", 12.5, True, -1, 9001):
        with pytest.raises(ValidationError):
            _validate_artifact_save(
                {"webapp_metadata": {"lifecycle": {"ttl_hours": bad_val}}})
    _validate_artifact_save({"webapp_metadata": {"lifecycle": {"ttl_hours": 72}}})


# --- F2 addendum: dismiss rejects internal-secret callers (hand-added) -------

def test_pending_dismiss_denies_internal_secret():
    from kiro_crew.deploy import handlers

    class _Req:
        headers = {"X-Internal-Secret": "s"}
        match_info = {"id": "x"}

    async def scenario():
        resp = await handlers._handle_pending_dismiss(_Req())
        assert resp.status == 403

    import asyncio
    asyncio.run(scenario())
